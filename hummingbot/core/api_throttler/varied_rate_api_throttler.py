import asyncio

from typing import Deque, List

from hummingbot.core.api_throttler.api_request_context_base import APIRequestContextBase
from hummingbot.core.api_throttler.api_throttler_base import APIThrottlerBase
from hummingbot.core.api_throttler.data_types import (
    RateLimit,
    Seconds,
    TaskLog
)


class VariedRateThrottler(APIThrottlerBase):

    def __init__(self,
                 rate_limit_list: List[RateLimit],
                 period_safety_margin: Seconds = 0.1,
                 retry_interval: Seconds = 0.1):
        super().__init__(rate_limit_list, period_safety_margin=period_safety_margin, retry_interval=retry_interval)

    def execute_task(self, path_url: str):
        rate_limit: RateLimit = self._path_rate_limit_map[path_url]

        return VariedRateRequestContext(
            task_logs=self._task_logs,
            rate_limit=rate_limit,
            stop_event=self._throttler_stop_event,
            period_safety_margin=self._period_safety_margin,
            retry_interval=self._retry_interval,
        )


class VariedRateRequestContext(APIRequestContextBase):

    def __init__(self,
                 task_logs: Deque[TaskLog],
                 rate_limit: RateLimit,
                 stop_event: asyncio.Event,
                 period_safety_margin: Seconds = 0.1,
                 retry_interval: Seconds = 0.1):
        super().__init__(
            task_logs,
            rate_limit,
            stop_event,
            period_safety_margin=period_safety_margin,
            retry_interval=retry_interval)

    def within_capacity(self) -> bool:
        current_capacity: int = self._rate_limit.limit - len([task
                                                              for task in self._task_logs
                                                              if task.path_url == self._rate_limit.path_url])

        return current_capacity - self._rate_limit.weight >= 0