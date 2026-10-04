from astrbot_sdk import MessageEvent, Plugin, on


class HelloPlugin(Plugin):
    @on.command("hello")
    async def hello(self, event: MessageEvent):
        yield event.reply(f"Hello, {event.sender.name}!")
