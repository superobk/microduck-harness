import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from microduck_harness.decisions import DecisionError, GLMClient, JevClient


OPTIONS = {"scan": "Look for the target with a bounded turn", "wait": "Remain stationary and get fresh evidence"}


def jev_result():
    return {
        "api_version": "agentjev.decision.v1",
        "results": [{"id": "0", "answers": [{
            "id": "next", "type": "choice", "value": "scan",
            "top_probability": .9, "margin": .8,
            "distribution": {"scan": .9, "wait": .1},
        }]}],
        "usage": {"generated_tokens": 0},
    }


def completion(content, finish_reason="stop"):
    return {"choices": [{"finish_reason": finish_reason, "message": {"role": "assistant", "content": content}}]}


@pytest.mark.asyncio
async def test_jev_uses_verified_native_contract_and_preserves_distribution():
    async def handler(request):
        assert request.url == "http://localhost:8149/api/evaluate"
        payload = json.loads(request.content)
        assert payload["state"] == {"goal": "找到纸团", "step": 1}
        assert payload["questions"][0]["id"] == "next"
        assert payload["questions"][0]["type"] == "choice"
        assert payload["questions"][0]["options"] == OPTIONS
        assert "Authorization" not in request.headers
        return httpx.Response(200, json=jev_result())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = JevClient("http://localhost:8149", client=http)
        result = await client.choose({"goal": "找到纸团", "step": 1}, OPTIONS)
        assert result.action == "scan" and result.source == "agentjev"
        assert result.probability == .9 and result.margin == .8
        assert result.distribution == {"scan": .9, "wait": .1}
        assert result.elapsed_ms >= 0
        await client.aclose()
        assert not http.is_closed  # Injected transport ownership stays with caller.


@pytest.mark.asyncio
async def test_jev_low_confidence_is_not_silently_promoted_to_success():
    data = jev_result()
    answer = data["results"][0]["answers"][0]
    answer.update(top_probability=.51, margin=.02, distribution={"scan": .51, "wait": .49})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data))) as http:
        result = await JevClient(client=http).choose({}, OPTIONS)
    assert result.probability == .51 and result.margin == .02


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(api_version="other"),
    lambda d: d["results"][0].update(id="old-result"),
    lambda d: d["results"][0]["answers"][0].update(id="stale-question"),
    lambda d: d["results"][0]["answers"][0].update(type="boolean"),
    lambda d: d["results"][0]["answers"].append(deepcopy(d["results"][0]["answers"][0])),
    lambda d: d["results"][0]["answers"][0].update(value="shell"),
    lambda d: d["results"][0]["answers"][0].update(value="wait"),
    lambda d: d["results"][0]["answers"][0].update(top_probability=.8),
    lambda d: d["results"][0]["answers"][0].update(margin=.5),
    lambda d: d["results"][0]["answers"][0].update(distribution={"scan": 1.1, "wait": -.1}),
    lambda d: d["results"][0]["answers"][0].update(distribution={"scan": .9}),
    lambda d: d["results"][0]["answers"][0].update(distribution={"scan": .9, "wait": .3}),
    lambda d: d["results"][0]["answers"][0].update(distribution={"scan": True, "wait": 0}),
    lambda d: d["usage"].update(truncated_inputs=1),
])
@pytest.mark.asyncio
async def test_jev_invalid_contract_never_becomes_an_action(mutation):
    data = jev_result()
    mutation(data)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data))) as http:
        with pytest.raises(DecisionError):
            await JevClient(client=http).choose({}, OPTIONS)


@pytest.mark.parametrize("body", [b'{"api_version":NaN}', b'{"api_version":"a","api_version":"b"}', b'not json'])
@pytest.mark.asyncio
async def test_nonfinite_duplicate_and_nonjson_responses_rejected(body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))) as http:
        with pytest.raises(DecisionError):
            await JevClient(client=http).choose({}, OPTIONS)


@pytest.mark.asyncio
async def test_total_timeout_has_no_unobserved_retry():
    count = 0

    async def handler(_):
        nonlocal count
        count += 1
        await asyncio.sleep(.1)
        return httpx.Response(200, json=jev_result())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(DecisionError, match="timed out"):
            await JevClient(client=http, timeout_s=.01).choose({}, OPTIONS)
    assert count == 1


@pytest.mark.asyncio
async def test_provider_errors_do_not_echo_credentials_or_response_bodies():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(401, text="secret-key"))) as http:
        with pytest.raises(DecisionError) as error:
            await GLMClient("http://localhost/v1", api_key="secret-key", client=http).choose({}, OPTIONS)
    assert str(error.value) == "Model endpoint returned HTTP 401"


@pytest.mark.asyncio
async def test_glm_private_endpoint_plan_payload_and_model_id():
    plan = {"kind": "find", "target": "paper", "max_steps": 40, "deadline_s": 120, "rationale": "Find the visible paper target"}

    async def handler(request):
        assert str(request.url) == "http://models.internal:8000/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-token"
        payload = json.loads(request.content)
        assert payload["model"] == "my-glm53-flash"
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["stream"] is False
        assert json.loads(payload["messages"][1]["content"])["instruction"] == "找到纸团"
        return httpx.Response(200, json=completion(json.dumps(plan)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await GLMClient("http://models.internal:8000/v1", model="my-glm53-flash", api_key="test-token", client=http).plan_goal("找到纸团", {"capabilities": ["find"]})
    assert result == plan


@pytest.mark.asyncio
async def test_glm_choice_has_no_invented_probability_and_optional_json_mode():
    async def handler(request):
        payload = json.loads(request.content)
        assert "response_format" not in payload
        assert "Authorization" not in request.headers
        assert json.loads(payload["messages"][1]["content"])["candidates"] == OPTIONS
        return httpx.Response(200, json=completion('```json\n{"action":"wait"}\n```'))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await GLMClient("http://localhost/v1/chat/completions", client=http, response_format=False).choose({}, OPTIONS)
    assert result.action == "wait" and result.source == "glm"
    assert result.probability is None and result.margin is None and result.distribution == {}


@pytest.mark.parametrize("value", [
    '{"action":"unknown"}', '{"action":"scan","vx":99}',
    'I think {"action":"scan"}', '["scan"]', '{"action":"scan","action":"wait"}',
])
@pytest.mark.asyncio
async def test_glm_cannot_inject_action_or_parameters(value):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion(value)))) as http:
        with pytest.raises(DecisionError):
            await GLMClient("http://localhost/v1", client=http).choose({}, OPTIONS)


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(kind="pickup"),
    lambda p: p.update(kind=[]),
    lambda p: p.update(max_steps=True),
    lambda p: p.update(max_steps=1000),
    lambda p: p.update(deadline_s=-1),
    lambda p: p.update(target=None),
    lambda p: p.update(extra="shell"),
])
@pytest.mark.asyncio
async def test_glm_rejects_malformed_or_out_of_scope_goals(mutation):
    plan = {"kind": "find", "target": "paper", "max_steps": 40, "deadline_s": 120, "rationale": "Find target"}
    mutation(plan)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion(json.dumps(plan))))) as http:
        with pytest.raises(DecisionError):
            await GLMClient("http://localhost/v1", client=http).plan_goal("Find paper", {})


@pytest.mark.parametrize("reason", ["length", "tool_calls", "content_filter"])
@pytest.mark.asyncio
async def test_glm_incomplete_generation_cannot_trigger_action(reason):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion('{"action":"scan"}', reason)))) as http:
        with pytest.raises(DecisionError):
            await GLMClient("http://localhost/v1", client=http).choose({}, OPTIONS)


@pytest.mark.asyncio
async def test_invalid_candidates_are_rejected_before_network_io():
    def handler(_):
        pytest.fail("invalid choices should not reach the network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        for candidates in ({"scan": "Only choice"}, {"a": "same", "b": "same"}):
            with pytest.raises(DecisionError):
                await JevClient(client=http).choose({}, candidates)


@pytest.mark.parametrize("url", ["file:///tmp/model", "http://key:secret@localhost", "http://localhost/v1?key=secret", "http://localhost:broken"])
def test_credential_leaking_and_non_http_urls_rejected(url):
    with pytest.raises(ValueError):
        GLMClient(url)
