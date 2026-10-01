"""Real encrypted callback boundary tests; no credentials or external API calls."""
import time
from urllib.parse import urlencode
from xml.etree import ElementTree as ET
from unittest.mock import Mock

import pytest
from aiohttp import StreamReader
from aiohttp.test_utils import make_mocked_request
from gateway.config import PlatformConfig
from plugins.platforms.wecom.callback_adapter import WecomCallbackAdapter
from plugins.platforms.wecom.wecom_crypto import WXBizMsgCrypt, WeComCryptoError


def app(name="a", corp="test-corp-a", agent="1001"):
    return dict(name=name, corp_id=corp, agent_id=agent, token="synthetic-token-"+name,
                encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG")


def adapter(apps):
    return WecomCallbackAdapter(PlatformConfig(enabled=True, extra={"apps": apps}))


def encrypted_request(config, *, age=0, recipient=None, agent_id=None, message_id="m1"):
    root=ET.Element("xml")
    for tag,value in {"ToUserName": recipient or config["corp_id"], "FromUserName":"owner",
                      "MsgType":"text", "Content":"Synthetic request", "MsgId":message_id,
                      "AgentID": agent_id or config["agent_id"]}.items():
        ET.SubElement(root,tag).text=value
    crypt=WXBizMsgCrypt(config["token"],config["encoding_aes_key"],config["corp_id"])
    envelope=ET.fromstring(crypt.encrypt(ET.tostring(root,encoding="unicode"),
                         timestamp=str(int(time.time())-age),nonce="synthetic-nonce"))
    query=urlencode({"msg_signature":envelope.findtext("MsgSignature"),
                     "timestamp":envelope.findtext("TimeStamp"),"nonce":envelope.findtext("Nonce")})
    reader=StreamReader(protocol=Mock(_reading_paused=False),limit=2**20)
    reader.feed_data(ET.tostring(envelope));reader.feed_eof()
    return make_mocked_request("POST","/wecom/callback?"+query,payload=reader)


@pytest.mark.asyncio
async def test_valid_callback_queued_once():
    conf=app(); a=adapter([conf])
    assert (await a._handle_callback(encrypted_request(conf))).status==200
    assert (await a._handle_callback(encrypted_request(conf))).status==200
    assert a._message_queue.qsize()==1


@pytest.mark.asyncio
@pytest.mark.parametrize("age",[301,3600,-301,-3600])
async def test_signed_but_stale_or_future_callback_is_rejected(age):
    conf=app(); a=adapter([conf])
    assert (await a._handle_callback(encrypted_request(conf,age=age))).status==403
    assert a._message_queue.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch",[{"recipient":"another-corp"},{"agent_id":"9999"}])
async def test_inner_recipient_must_match_authenticated_app(mismatch):
    conf=app(); a=adapter([conf])
    assert (await a._handle_callback(encrypted_request(conf,**mismatch))).status==400
    assert a._message_queue.empty()


@pytest.mark.asyncio
async def test_message_ids_do_not_collide_across_apps():
    first=app();second=app("b","test-corp-b","2002");a=adapter([first,second])
    assert (await a._handle_callback(encrypted_request(first))).status==200
    assert (await a._handle_callback(encrypted_request(second))).status==200
    assert a._message_queue.qsize()==2


def test_invalid_signature_is_domain_error_not_type_error():
    conf=app();crypt=WXBizMsgCrypt(conf["token"],conf["encoding_aes_key"],conf["corp_id"])
    with pytest.raises(WeComCryptoError):
        crypt.decrypt("non-ascii-签名","1","nonce","invalid")
