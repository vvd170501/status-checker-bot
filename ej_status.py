from time import time, sleep

import requests


class Checker:
    def __init__(self, url, timeout, interval, notifier):
        self.url = url
        self.timeout = timeout
        self.interval = interval
        self.notifier = notifier
        self.session = requests.Session()
        self.available = None

    def run(self):
        while True:
            t_next = time() + self.interval
            try:
                _ = self.session.get(self.url, timeout=self.timeout)
            except requests.exceptions.RequestException:
                available = False
            else:
                available = True
            if available != self.available and self.available is not None:
                self.notifier.notify('✅ Ejudge is up(?)' if available else '❌ Ejudge is down')
            self.available = available
            sleep(max(0, t_next - time()))


class Notifier:
    def __init__(self, token, channel):
        self.token = token
        self.channel = channel
        self.session = requests.Session()

    def notify(self, msg):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = {'chat_id': self.channel, 'text': msg}
        r = self.session.post(url, data=data)


def main():
    timeout = 5
    interval = 30

    token = 'TELEGRAM_BOT_TOKEN'
    channel = -1001234567890

    notifier = Notifier(token, channel)
    checker = Checker('https://caos.ejudge.ru/', timeout, interval, notifier)
    checker.run()

if __name__ == '__main__':
    main()
