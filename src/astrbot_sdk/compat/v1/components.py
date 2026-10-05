"""Legacy message components mirroring the old astrbot.api component shapes.

These classes are thin facades over the new SDK message segments: they expose
the old field names and constructors, and convert to the new immutable DTOs
at the protocol boundary.
"""

from __future__ import annotations

import base64
import enum
from pathlib import Path
from typing import Any

from ...message_components import (
    At as SDKAt,
)
from ...message_components import (
    AtAll as SDKAtAll,
)
from ...message_components import (
    Face as SDKFace,
)
from ...message_components import (
    File as SDKFile,
)
from ...message_components import (
    Forward as SDKForward,
)
from ...message_components import (
    Image as SDKImage,
)
from ...message_components import (
    MessageSegment,
)
from ...message_components import (
    Node as SDKNode,
)
from ...message_components import (
    Nodes as SDKNodes,
)
from ...message_components import (
    Plain as SDKPlain,
)
from ...message_components import (
    Record as SDKRecord,
)
from ...message_components import (
    Reply as SDKReply,
)
from ...message_components import (
    UnknownSegment as SDKUnknownSegment,
)
from ...message_components import (
    Video as SDKVideo,
)
from ...messages import MessageChain as SDKMessageChain
from ...results import MessageResult as SDKMessageResult
from ...results import Propagation


def _media_source(url: Any, file: Any) -> Any:
    """Resolve a legacy media source into a form the outbound uploader accepts.

    Local path strings become ``Path`` objects (uploaded via assets.transfer);
    ``base64://`` strings are decoded to bytes. Public URLs pass through.

    Args:
        url: Legacy ``url`` field, preferred when set.
        file: Legacy ``file`` field fallback.

    Returns:
        A URL string, ``Path``, or bytes suitable as a media segment source.
    """
    source = url or file
    if isinstance(source, str):
        if source.startswith("base64://"):
            return base64.b64decode(source[len("base64://") :])
        if source and not source.startswith(("http://", "https://")):
            return Path(source)
    return source


def to_sdk_segment(component: Any) -> MessageSegment:
    """Convert one compat component into the new SDK segment."""
    if isinstance(component, Plain):
        return SDKPlain(component.text)
    if isinstance(component, AtAll):
        return SDKAtAll()
    if isinstance(component, At):
        return SDKAt(user_id=str(component.qq), name=component.name or None)
    if isinstance(component, Reply):
        return SDKReply(
            id=str(component.id),
            sender_id=str(component.sender_id),
            sender_name=component.sender_nickname or None,
            text=component.message_str or None,
        )
    if isinstance(component, Image):
        return SDKImage(source=_media_source(component.url, component.file))
    if isinstance(component, Record):
        return SDKRecord(source=_media_source(component.url, component.file))
    if isinstance(component, Video):
        return SDKVideo(source=_media_source(component.url, component.file))
    if isinstance(component, Face):
        return SDKFace(id=int(component.id))
    if isinstance(component, Forward):
        return SDKForward(id=str(component.id))
    if isinstance(component, File):
        return SDKFile(
            source=_media_source(component.url, component.file_),
            filename=component.name or None,
        )
    if isinstance(component, Nodes):
        return SDKNodes([to_sdk_segment(node) for node in component.nodes])
    if isinstance(component, Node):
        return SDKNode(
            sender_id=str(component.uin or "0"),
            sender_name=component.name or None,
            content=[to_sdk_segment(c) for c in component.content],
        )
    if isinstance(component, Poke):
        return SDKUnknownSegment(
            segment_type="Poke",
            data={
                "user_id": str(component.user_id or ""),
                "poke_type": str(component.poke_type or "126"),
            },
        )
    if isinstance(component, Json):
        return SDKUnknownSegment(
            segment_type="Json",
            data={"data": component.data},
        )
    if isinstance(component, Share):
        return SDKUnknownSegment(
            segment_type="Share",
            data={
                "url": component.url,
                "title": component.title,
                "content": component.content or "",
                "image": component.image or "",
            },
        )
    raise TypeError(f"unsupported message component: {type(component)!r}")


def from_sdk_segment(segment: MessageSegment) -> Any:
    """Convert one SDK segment back into the compat component."""
    if isinstance(segment, SDKPlain):
        return Plain(segment.text)
    if isinstance(segment, SDKAtAll):
        return AtAll()
    if isinstance(segment, SDKAt):
        return At(qq=segment.user_id, name=segment.name or "")
    if isinstance(segment, SDKReply):
        return Reply(
            id=segment.id,
            sender_id=segment.sender_id or "",
            sender_nickname=segment.sender_name or "",
            message_str=segment.text or "",
        )
    if isinstance(segment, SDKImage):
        source = segment.source
        if isinstance(source, str):
            return Image(file=source, url=source)
        return Image(file=source)
    if isinstance(segment, SDKRecord):
        source = segment.source
        if isinstance(source, str):
            return Record(file=source, url=source)
        return Record(file=source)
    if isinstance(segment, SDKVideo):
        source = segment.source
        if isinstance(source, str):
            return Video(file=source, url=source)
        return Video(file=source)
    if isinstance(segment, SDKFace):
        return Face(id=segment.id)
    if isinstance(segment, SDKForward):
        return Forward(id=segment.id)
    if isinstance(segment, SDKFile):
        source = segment.source
        file_comp = File(name=segment.filename or "")
        if isinstance(source, str):
            file_comp.url = source
        else:
            file_comp.file_ = source
        return file_comp
    if isinstance(segment, SDKNodes):
        return Nodes([from_sdk_segment(node) for node in segment.nodes])
    if isinstance(segment, SDKNode):
        return Node(
            content=[from_sdk_segment(c) for c in segment.content],
            uin=segment.sender_id,
            name=segment.sender_name or "",
        )
    if isinstance(segment, SDKUnknownSegment):
        if segment.segment_type == "Poke":
            return Poke(
                user_id=segment.data.get("user_id"),
                poke_type=segment.data.get("poke_type", "126"),
            )
        if segment.segment_type == "Json":
            return Json(data=segment.data.get("data"))
        if segment.segment_type == "Share":
            return Share(
                url=str(segment.data.get("url", "")),
                title=str(segment.data.get("title", "")),
                content=str(segment.data.get("content", "")),
                image=str(segment.data.get("image", "")),
            )
    return UnknownComponent(segment)


class UnknownComponent:
    """Compat placeholder for segments without a legacy counterpart."""

    def __init__(self, segment: MessageSegment) -> None:
        self.segment = segment
        self.type = getattr(segment, "type", "unknown")


class BaseMessageComponent:
    """Legacy base component; structural typing covers the real usage."""

    type: str = "unknown"

    def toDict(self) -> dict:
        return {"type": self.type, "data": {}}


class ComponentType(enum.Enum):
    """Legacy component type enum (labels only)."""

    Plain = "Plain"
    Face = "Face"
    Record = "Record"
    Video = "Video"
    At = "At"
    AtAll = "AtAll"
    RPS = "RPS"
    Dice = "Dice"
    Shake = "Shake"
    Share = "Share"
    Contact = "Contact"
    Location = "Location"
    Music = "Music"
    Image = "Image"
    Reply = "Reply"
    Poke = "Poke"
    Forward = "Forward"
    Node = "Node"
    Nodes = "Nodes"
    Json = "Json"
    Unknown = "Unknown"
    File = "File"


class Plain:
    """Legacy Plain component."""

    def __init__(self, text: str, **_: Any) -> None:
        self.text = text

    def toDict(self) -> dict:
        return {"type": "text", "data": {"text": self.text}}


class At:
    """Legacy At component; qq carries the user id."""

    def __init__(self, qq: Any = None, name: str = "", **_: Any) -> None:
        self.qq = qq
        self.name = name

    def toDict(self) -> dict:
        return {"type": "at", "data": {"qq": str(self.qq)}}


class AtAll(At):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(qq="all", **kwargs)


class Reply:
    """Legacy Reply component."""

    def __init__(
        self,
        id: Any = "",
        sender_id: Any = "",
        sender_nickname: str = "",
        message_str: str = "",
        **_: Any,
    ) -> None:
        self.id = id
        self.sender_id = sender_id
        self.sender_nickname = sender_nickname
        self.message_str = message_str

    def toDict(self) -> dict:
        return {"type": "reply", "data": {"id": str(self.id)}}


class Image:
    """Legacy Image component with the old file/url/path field trio."""

    def __init__(
        self,
        file: Any = "",
        url: str | None = "",
        path: str | None = "",
        **_: Any,
    ) -> None:
        self.file = file
        self.url = url
        self.path = path

    @staticmethod
    def fromURL(url: str, **kwargs: Any) -> Image:
        return Image(file=url, url=url, **kwargs)

    @staticmethod
    def fromFileSystem(path: Any, **kwargs: Any) -> Image:
        return Image(file=str(path), path=str(path), **kwargs)

    @staticmethod
    def fromBytes(data: bytes, **kwargs: Any) -> Image:
        return Image(file=data, **kwargs)

    def toDict(self) -> dict:
        return {"type": "image", "data": {"file": self.file}}


class Record:
    """Legacy Record (voice) component."""

    def __init__(self, file: Any = "", url: str | None = "", **_: Any) -> None:
        self.file = file
        self.url = url


class Video:
    """Legacy Video component."""

    def __init__(self, file: Any = "", url: str | None = "", **_: Any) -> None:
        self.file = file
        self.url = url


class Face:
    """Legacy Face (platform emoji) component."""

    def __init__(self, id: int = 0, **_: Any) -> None:
        self.id = id


class Forward:
    """Legacy Forward (merged-forward reference) component."""

    def __init__(self, id: str = "", **_: Any) -> None:
        self.id = id


class File:
    """Legacy File message segment."""

    def __init__(self, name: str = "", file: str = "", url: str = "") -> None:
        self.name = name
        self.file_ = file
        self.url = url

    @property
    def file(self) -> Any:
        return self.url or self.file_


class Node:
    """Legacy merged-forward node."""

    def __init__(
        self,
        content: Any,
        id: Any = 0,
        name: str = "",
        uin: Any = "0",
        seq: Any = "",
        time: Any = 0,
        **_: Any,
    ) -> None:
        if isinstance(content, Node):
            content = [content]
        self.id = id
        self.name = name
        self.uin = uin
        self.content: list = list(content or [])
        self.seq = seq
        self.time = time


class Nodes:
    """Legacy merged-forward node list."""

    def __init__(self, nodes: list | None = None, **_: Any) -> None:
        self.nodes: list = list(nodes or [])


class Poke:
    """Legacy poke component (carried as an unknown segment over the wire)."""

    def __init__(
        self,
        user_id: Any = None,
        poke_type: str = "126",
        **_: Any,
    ) -> None:
        self.user_id = user_id
        self.poke_type = poke_type


class Json:
    """Legacy JSON card component."""

    def __init__(self, data: Any = None, **_: Any) -> None:
        self.data = data


class Share:
    """Legacy link-share card component."""

    def __init__(
        self,
        url: str = "",
        title: str = "",
        content: str = "",
        image: str = "",
        **_: Any,
    ) -> None:
        self.url = url
        self.title = title
        self.content = content
        self.image = image


class MessageChain:
    """Legacy mutable message chain."""

    def __init__(self, chain: list | None = None, **_: Any) -> None:
        self.chain: list = list(chain or [])

    def message(self, text: str) -> MessageChain:
        self.chain.append(Plain(text))
        return self

    def get_plain_text(self) -> str:
        return "".join(c.text for c in self.chain if isinstance(c, Plain))

    def to_sdk(self) -> SDKMessageChain:
        """Convert to the new immutable chain."""
        return SDKMessageChain(*[to_sdk_segment(c) for c in self.chain])

    @staticmethod
    def from_sdk(chain: SDKMessageChain) -> MessageChain:
        """Build a compat chain from the new SDK chain."""
        return MessageChain(chain=[from_sdk_segment(s) for s in chain])


class MessageEventResult:
    """Legacy message event result."""

    def __init__(self) -> None:
        self.chain: list = []
        self._stopped = False

    def message(self, text: str) -> MessageEventResult:
        self.chain = [Plain(text)]
        return self

    def set_chain(self, chain: MessageChain | list) -> MessageEventResult:
        self.chain = list(chain.chain if isinstance(chain, MessageChain) else chain)
        return self

    def stop_event(self) -> MessageEventResult:
        self._stopped = True
        return self

    def continue_event(self) -> MessageEventResult:
        self._stopped = False
        return self

    def is_stopped(self) -> bool:
        return self._stopped

    def get_plain_text(self) -> str:
        return "".join(c.text for c in self.chain if isinstance(c, Plain))

    def to_sdk_result(self) -> SDKMessageResult | None:
        """Convert to the new result; None when empty and not stopped."""
        propagation = Propagation.STOP if self._stopped else Propagation.CONTINUE
        if not self.chain:
            if self._stopped:
                from ...results import EventResult

                return EventResult(propagation=propagation)
            return None
        return SDKMessageResult(
            propagation=propagation,
            message=MessageChain(chain=self.chain).to_sdk(),
        )
