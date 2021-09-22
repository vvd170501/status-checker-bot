import asyncio
from urllib.parse import urlsplit

import aiohttp
import sys
import socket
from datetime import datetime
from enum import Enum, auto
from time import time


def _network_available(host, port, timeout):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        return True
    except socket.error:
        return False


async def network_available(host='1.1.1.1', port=53, timeout=3):
    # TODO use asyncio.open_connection?
    return await asyncio.get_running_loop().run_in_executor(None,
                                                            _network_available, host, port, timeout)


class RequestStatus(Enum):
    NONE = auto()
    OK = auto()
    TIMEOUT = auto()
    CONNECTION_ERR = auto()
    HTTP_ERR = auto()
    OTHER = auto()


class SiteStatus(Enum):
    UNINITIALIZED = auto()
    UP = auto()
    DOWN = auto()


class Checker:
    def __init__(
            self, session, url, notifier, *,
            # up->down when response time exceeds max_timeout
            timeout=5, timeout_multiplier=2.5, max_timeout=30,
            # n requests without timeout or connection error -> try to decrease
            timeout_decrease_after=5,
            # down->up: recheck n times with the specified intervals (default: once after 10 sec)
            ok_recheck=1, ok_recheck_intervals=None,
            # up->down: recheck on connection error or http error (default: 10s and additional 20s)
            fail_recheck=2, fail_recheck_intervals=None,
            # the default interval (when up/down status is not changing)
            interval=45,
    ):
        self.session = session
        self.url = url
        self.notifier = notifier
        self._domain = urlsplit(url).netloc

        self.timeout = timeout
        self.timeout_multiplier = timeout_multiplier
        self.max_timeout = max_timeout
        self.timeout_decrease_after = timeout_decrease_after

        self.ok_recheck = ok_recheck
        self.ok_recheck_intervals = ok_recheck_intervals
        if self.ok_recheck_intervals is None:
            self.ok_recheck_intervals = [10] * self.ok_recheck
        self.fail_recheck = fail_recheck
        self.fail_recheck_intervals = fail_recheck_intervals
        if self.fail_recheck_intervals is None:  # will work for fail_recheck <= 2
            self.fail_recheck_intervals = [10, 20]

        self.interval = interval

        self._last_request = 0
        self.request_status = RequestStatus.NONE
        self.site_status = SiteStatus.UNINITIALIZED

        self._ok_count = 0
        self._fail_count = 0
        self._timeout_mul_pow = 0
        self._timeout_decrease = 0

    @property
    def delta(self):
        if self.request_status is RequestStatus.NONE:
            return 0
        if self.request_status is RequestStatus.OK:
            if self.site_status is SiteStatus.UP:
                return self.interval
            # is down / not initialized
            return self.ok_recheck_intervals[self._ok_count - 1]
        if self.request_status is RequestStatus.TIMEOUT:
            if self.timeout == self.max_timeout:
                return self.interval
            return 0  # increase the timeout and retry
        if self.site_status is SiteStatus.DOWN:
            return self.interval
        # is up / not initialized
        return self.fail_recheck_intervals[self._fail_count - 1]

    @property
    def timeout(self):
        return min(self._timeout * self.timeout_multiplier**self._timeout_mul_pow,
                   self.max_timeout)

    @timeout.setter
    def timeout(self, value):
        self._timeout = value

    @property
    def site_status(self):
        return self._site_status

    @site_status.setter
    def site_status(self, value):
        if value is SiteStatus.UNINITIALIZED:
            self._site_status = value
            return

        old_value = self._site_status
        self._site_status = value
        if old_value is SiteStatus.UNINITIALIZED or old_value == value:
            return

        self._ok_count = 0
        self._fail_count = 0

        if value is SiteStatus.UP:
            status = f'✅ {self._domain} is up'
        else:
            status = f'❌ {self._domain} is down'
        asyncio.create_task(self.notifier.notify(status))  # cleanup at exit?

    async def update_status(self, value):
        self.request_status = value
        if value is RequestStatus.NONE:
            return

        # handle timeouts
        if value is RequestStatus.TIMEOUT:
            # filter local problems (timeout won't be increased)
            # assuming minimal timeout is not too low, so the system will not get overloaded
            if not await network_available():
                return
            self._ok_count = 0
            self._fail_count = 0  # not sure about this
            self._try_increase_timeout()
            return
        if value is RequestStatus.OK or value is RequestStatus.HTTP_ERR:
            self._try_decrease_timeout()

        # to set initial status, we need N consecutive requests with the same status
        # same rules apply to up <-> down status changes
        if value is RequestStatus.OK:
            if self.site_status in [SiteStatus.UP, SiteStatus.UNINITIALIZED]:
                # filter temporary failures
                self._fail_count = 0
            if self.site_status in [SiteStatus.DOWN, SiteStatus.UNINITIALIZED]:
                self._ok_count += 1
                if self._ok_count == self.ok_recheck + 1:
                    self.site_status = SiteStatus.UP
        else:  # Connection error / HTTP error / other
            # filter local problems
            if value is not RequestStatus.HTTP_ERR and not await network_available():
                return
            if self.site_status in [SiteStatus.DOWN, SiteStatus.UNINITIALIZED]:
                # filter short uptime intervals
                self._ok_count = 0
            if self.site_status in [SiteStatus.UP, SiteStatus.UNINITIALIZED]:
                self._fail_count += 1
                if self._fail_count == self.fail_recheck + 1:
                    self.site_status = SiteStatus.DOWN

    def _try_decrease_timeout(self):
        if self._timeout_mul_pow == 0:
            return
        self._timeout_decrease -= 1
        if self._timeout_decrease == 0:
            self._timeout_mul_pow -= 1
            self._timeout_decrease = self.timeout_decrease_after

    def _try_increase_timeout(self):
        sys.stderr.write(f'Timed out ({round(self.timeout, 2)} s)\n'); sys.stderr.flush()
        if self.timeout == self.max_timeout:
            self.site_status = SiteStatus.DOWN
            return
        self._timeout_mul_pow += 1
        self._timeout_decrease = self.timeout_decrease_after

    async def _get_status(self):
        self._last_request = time()
        try:
            async with self.session.get(self.url,
                                        timeout=aiohttp.ClientTimeout(total=self.timeout),
                                        ssl=False) as resp:
                if resp.ok:
                    return RequestStatus.OK
                return RequestStatus.HTTP_ERR
        except asyncio.TimeoutError:
            return RequestStatus.TIMEOUT
        except aiohttp.ClientConnectionError:
            if self.site_status in [SiteStatus.UP, SiteStatus.UNINITIALIZED]:
                sys.stderr.write(f'Connection error\n'); sys.stderr.flush()
            return RequestStatus.CONNECTION_ERR
        except aiohttp.ClientError as e:
            sys.stderr.write(f'WTF: {e}\n'); sys.stderr.flush()
            return RequestStatus.OTHER

    async def run(self):
        try:
            while True:
                await asyncio.sleep(max(0.0, self._last_request + self.delta - time()))
                await self.update_status(await self._get_status())
        except KeyboardInterrupt:
            return


class Notifier:
    def __init__(self, session, token, channel):
        self.session = session
        self.token = token
        self.channel = channel

    async def notify(self, msg):
        url = f'https://api.telegram.org/bot{self.token}/sendMessage'
        data = {'chat_id': self.channel, 'text': msg}
        try:
            await self.session.post(url, data=data)
        except Exception as e:
            sys.stderr.write(f'Cannot post an update: {e}\n')


class LMSChecker(Checker):
    """Increases timeout around 3:50 for lms.hse.ru.

    There are lots of false positives when using default timeouts.
    """

    @property
    def timeout(self):
        timeout = super().timeout
        now = datetime.now()
        if (3, 35) <= (now.hour, now.minute) <= (4, 5):
            return max(timeout, 60)
        return timeout

    @timeout.setter
    def timeout(self, value):
        Checker.timeout.fset(self, value)


async def main():
    token = 'TELEGRAM_BOT_TOKEN'
    channel = -1001234567890

    notifier_session = aiohttp.ClientSession()
    checker_session = aiohttp.ClientSession()
    notifier = Notifier(notifier_session, token, channel)

    main_checker = LMSChecker(checker_session, 'https://lms.hse.ru/', notifier)
    auth_checker = Checker(
        checker_session,
        'https://lk.hse.ru/signin?redirecturl=https://lms.hse.ru/elk_auth.php&systemid=19',
        notifier
    )
    await asyncio.gather(main_checker.run(), auth_checker.run())
    # cleanup
    await asyncio.gather(notifier_session.close(), checker_session.close())


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
