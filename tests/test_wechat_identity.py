from dataclasses import dataclass

from src.agent.wechat_identity import parse_profile_identity


@dataclass
class Block:
    text: str
    x: int
    y: int
    confidence: float = 0.99


def test_parse_profile_identity_extracts_nickname_and_wechat_id() -> None:
    identity = parse_profile_identity(
        [
            Block("番石榴", 180, 55),
            Block("微信号：higuava001", 220, 95),
            Block("发消息", 200, 140),
        ]
    )

    assert identity is not None
    assert identity.nickname == "番石榴"
    assert identity.wechat_id == "higuava001"


def test_parse_profile_identity_rejects_labels_without_nickname() -> None:
    assert parse_profile_identity([Block("微信号：wxid_demo", 180, 90)]) is None
