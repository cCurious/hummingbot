#!/usr/bin/env python
import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp
import hummingbot.connector.exchange.crypto_com.crypto_com_constants as CONSTANTS
import hummingbot.connector.exchange.crypto_com.crypto_com_utils as crypto_com_utils
import pandas as pd
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.utils.async_utils import safe_gather
from hummingbot.logger import HummingbotLogger

from .crypto_com_active_order_tracker import CryptoComActiveOrderTracker
from .crypto_com_order_book import CryptoComOrderBook
from .crypto_com_websocket import CryptoComWebsocket


class CryptoComAPIOrderBookDataSource(OrderBookTrackerDataSource):
    MAX_RETRIES = 20
    MESSAGE_TIMEOUT = 30.0
    SNAPSHOT_TIMEOUT = 10.0

    _logger: Optional[HummingbotLogger] = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    def __init__(self, trading_pairs: List[str] = None, throttler: Optional[AsyncThrottler] = None):
        super().__init__(trading_pairs)
        self._trading_pairs: List[str] = trading_pairs
        self._snapshot_msg: Dict[str, any] = {}
        self._throttler = throttler or self._get_throttler_instance()

    @classmethod
    def _get_throttler_instance(cls):
        return AsyncThrottler(CONSTANTS.RATE_LIMITS)

    @classmethod
    async def get_last_traded_prices(
        cls, trading_pairs: List[str], throttler: Optional[AsyncThrottler] = None
    ) -> Dict[str, float]:
        result = {}
        throttler = throttler or cls._get_throttler_instance()
        async with aiohttp.ClientSession() as client:
            async with throttler.execute_task(CONSTANTS.GET_TICKER_PATH_URL):
                resp = await client.get(crypto_com_utils.get_rest_url(path_url=CONSTANTS.GET_TICKER_PATH_URL))
                resp_json = await resp.json()
                for t_pair in trading_pairs:
                    last_trade = [
                        o["a"]
                        for o in resp_json["result"]["data"]
                        if o["i"] == crypto_com_utils.convert_to_exchange_trading_pair(t_pair)
                    ]
                    if last_trade and last_trade[0] is not None:
                        result[t_pair] = last_trade[0]
        return result

    @staticmethod
    async def fetch_trading_pairs(throttler: Optional[AsyncThrottler] = None) -> List[str]:
        """
        Retrieves active trading pairs using the exchange's REST API.
        :param throttler: Optional AsyncThrottler used to throttle the API request
        """
        throttler = throttler or CryptoComAPIOrderBookDataSource._get_throttler_instance()
        async with aiohttp.ClientSession() as client:
            async with throttler.execute_task(CONSTANTS.GET_TICKER_PATH_URL):
                url = crypto_com_utils.get_rest_url(path_url=CONSTANTS.GET_TICKER_PATH_URL)
                async with client.get(url=url, timeout=10) as response:
                    if response.status == 200:
                        try:
                            data: Dict[str, Any] = await response.json()
                            return [
                                crypto_com_utils.convert_from_exchange_trading_pair(item["i"])
                                for item in data["result"]["data"]
                            ]
                        except Exception:
                            pass
                            # Do nothing if the request fails -- there will be no autocomplete for kucoin trading pairs
                    return []

    @staticmethod
    async def get_order_book_data(trading_pair: str, throttler: Optional[AsyncThrottler] = None) -> Dict[str, any]:
        """
        Retrieves the JSON order book data of the specified trading pair using the exchange's REST API.
        :param trading_pair: Specified trading pair.
        :param throttler: Optional AsyncThrottler used to throttle the API request.
        """
        throttler = throttler or CryptoComAPIOrderBookDataSource._get_throttler_instance()
        async with aiohttp.ClientSession() as client:
            async with throttler.execute_task(CONSTANTS.GET_ORDER_BOOK_PATH_URL):
                url = crypto_com_utils.get_rest_url(CONSTANTS.GET_ORDER_BOOK_PATH_URL)
                params = {
                    "depth": 150,
                    "instrument_name": crypto_com_utils.convert_to_exchange_trading_pair(trading_pair),
                }
                orderbook_response = await client.get(url=url, params=params)

                if orderbook_response.status != 200:
                    raise IOError(
                        f"Error fetching OrderBook for {trading_pair} at {CONSTANTS.EXCHANGE_NAME}. "
                        f"HTTP status is {orderbook_response.status}."
                    )

                orderbook_data: List[Dict[str, Any]] = await safe_gather(orderbook_response.json())
                orderbook_data = orderbook_data[0]["result"]["data"][0]

            return orderbook_data

    async def get_new_order_book(self, trading_pair: str) -> OrderBook:
        snapshot: Dict[str, Any] = await self.get_order_book_data(trading_pair, self._throttler)
        snapshot_timestamp: int = crypto_com_utils.ms_timestamp_to_s(snapshot["t"])
        snapshot_msg: OrderBookMessage = CryptoComOrderBook.snapshot_message_from_exchange(
            snapshot, snapshot_timestamp, metadata={"trading_pair": trading_pair}
        )
        order_book = self.order_book_create_function()
        active_order_tracker: CryptoComActiveOrderTracker = CryptoComActiveOrderTracker()
        bids, asks = active_order_tracker.convert_snapshot_message_to_order_book_row(snapshot_msg)
        order_book.apply_snapshot(bids, asks, snapshot_msg.update_id)
        return order_book

    async def listen_for_trades(self, ev_loop: asyncio.BaseEventLoop, output: asyncio.Queue):
        """
        Listen for trades using websocket trade channel
        """
        while True:
            try:
                ws = CryptoComWebsocket()
                await ws.connect()

                await ws.subscribe([
                    f"trade.{crypto_com_utils.convert_to_exchange_trading_pair(pair)}"
                    for pair in self._trading_pairs
                ])

                async for response in ws.on_message():
                    if response.get("result") is None:
                        continue

                    for trade in response["result"]["data"]:
                        trade: Dict[Any] = trade
                        trade_timestamp: int = crypto_com_utils.ms_timestamp_to_s(trade["t"])
                        trade_msg: OrderBookMessage = CryptoComOrderBook.trade_message_from_exchange(
                            trade,
                            trade_timestamp,
                            metadata={"trading_pair": crypto_com_utils.convert_from_exchange_trading_pair(trade["i"])},
                        )
                        output.put_nowait(trade_msg)

            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error.", exc_info=True)
                await self._sleep(5.0)
            finally:
                await ws.disconnect()

    async def listen_for_order_book_diffs(self, ev_loop: asyncio.BaseEventLoop, output: asyncio.Queue):
        """
        Listen for orderbook diffs using websocket book channel
        """
        while True:
            try:
                ws = CryptoComWebsocket()
                await ws.connect()

                await ws.subscribe([
                    f"book.{crypto_com_utils.convert_to_exchange_trading_pair(pair)}.150"
                    for pair in self._trading_pairs
                ])

                async for response in ws.on_message():
                    if response.get("result") is None:
                        continue

                    order_book_data = response["result"]["data"][0]
                    timestamp: int = crypto_com_utils.ms_timestamp_to_s(order_book_data["t"])
                    # data in this channel is not order book diff but the entire order book (up to depth 150).
                    # so we need to convert it into a order book snapshot.
                    # Crypto.com does not offer order book diff ws updates.
                    orderbook_msg: OrderBookMessage = CryptoComOrderBook.snapshot_message_from_exchange(
                        order_book_data,
                        timestamp,
                        metadata={
                            "trading_pair": crypto_com_utils.convert_from_exchange_trading_pair(
                                response["result"]["instrument_name"]
                            )
                        },
                    )
                    output.put_nowait(orderbook_msg)

            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().network(
                    "Unexpected error with WebSocket connection.",
                    exc_info=True,
                    app_warning_msg="Unexpected error with WebSocket connection. Retrying in 30 seconds. "
                                    "Check network connection."
                )
                await self._sleep(30.0)
            finally:
                await ws.disconnect()

    async def listen_for_order_book_snapshots(self, ev_loop: asyncio.BaseEventLoop, output: asyncio.Queue):
        """
        Listen for orderbook snapshots by fetching orderbook
        """
        while True:
            try:
                for trading_pair in self._trading_pairs:
                    try:
                        snapshot: Dict[str, any] = await self.get_order_book_data(trading_pair, self._throttler)
                        snapshot_timestamp: int = crypto_com_utils.ms_timestamp_to_s(snapshot["t"])
                        snapshot_msg: OrderBookMessage = CryptoComOrderBook.snapshot_message_from_exchange(
                            snapshot, snapshot_timestamp, metadata={"trading_pair": trading_pair}
                        )
                        output.put_nowait(snapshot_msg)
                        self.logger().debug(f"Saved order book snapshot for {trading_pair}")
                        # Be careful not to go above API rate limits.
                        await self._sleep(5.0)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self.logger().network(
                            "Unexpected error with WebSocket connection.",
                            exc_info=True,
                            app_warning_msg="Unexpected error with WebSocket connection. Retrying in 5 seconds. "
                                            "Check network connection."
                        )
                        await self._sleep(5.0)
                this_hour: pd.Timestamp = pd.Timestamp.utcnow().replace(minute=0, second=0, microsecond=0)
                next_hour: pd.Timestamp = this_hour + pd.Timedelta(hours=1)
                delta: float = next_hour.timestamp() - time.time()
                await self._sleep(delta)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error.", exc_info=True)
                await self._sleep(5.0)
