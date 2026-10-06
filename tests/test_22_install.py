"""Spec 22: other workspaces install Knappy through OAuth, and one process serves all of them."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from knappy.db.repository import SqliteRepository
from knappy.fleet import Fleet
from knappy.mcp.auth import PendingAuth, seal_state
from knappy.mcp.hub import state_workspace
from knappy.mcp.store import derive_fernet
from knappy.slack.install import BOT_SCOPES, USER_SCOPES, InstallError, InstallFlow, add_install_routes
from knappy.slack.installations import Installation, Installations

MANIFEST = Path(__file__).resolve().parents[1] / "slack" / "manifest.yml"
NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
ALPHA = Installation("T_ALPHA", "Alpha", "xoxb-alpha", "B_ALPHA", "U_ALPHA", "xoxp-alpha")
BETA = Installation("T_BETA", "Beta", "xoxb-beta", "B_BETA", "U_BETA", None)


class FakeOAuth:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def oauth_v2_access(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response


def oauth_response(**overrides: Any) -> dict[str, Any]:
    return {
        "ok": True, "app_id": "A_KNAPPY", "access_token": "xoxb-new", "bot_user_id": "B_NEW",
        "team": {"id": "T_NEW", "name": "New Co"}, "authed_user": {"id": "U_INSTALLER", "access_token": "xoxp-new"},
        "is_enterprise_install": False, **overrides,
    }


def flow(client: FakeOAuth, clock=lambda: NOW) -> InstallFlow:
    return InstallFlow(
        client_id="123.456", client_secret="shh", public_url="https://knappy.example/", secret_key="test-secret",
        clock=clock, client=client,
    )


def test_install_requests_the_manifest_scopes() -> None:
    scopes = yaml.safe_load(MANIFEST.read_text())["oauth_config"]["scopes"]
    assert set(BOT_SCOPES) == set(scopes["bot"])
    assert set(USER_SCOPES) == set(scopes["user"])


def test_manifest_redirects_to_the_install_route() -> None:
    manifest = yaml.safe_load(MANIFEST.read_text())
    assert all(url.endswith("/slack/oauth_redirect") for url in manifest["oauth_config"]["redirect_urls"])
    assert "app_uninstalled" in manifest["settings"]["event_subscriptions"]["bot_events"]


async def test_tokens_are_encrypted_at_rest_and_revocable(repo: SqliteRepository) -> None:
    store = Installations(repo, "test-secret")
    await store.save(ALPHA)
    await store.save(BETA)
    raw = await repo.get_workspace("T_ALPHA")
    assert "xoxb-alpha" not in str(raw) and "xoxp-alpha" not in str(raw)
    assert await store.get("T_ALPHA") == ALPHA
    assert await store.get("T_BETA") == BETA
    assert await Installations(repo, "another-secret").get("T_ALPHA") is None

    await repo.ensure_workspace("T_ALPHA", "Renamed")
    assert await store.get("T_ALPHA") == ALPHA

    await store.revoke("T_ALPHA")
    assert await store.get("T_ALPHA") is None
    assert [installation.team_id for installation in await store.all()] == ["T_BETA"]
    # The conftest workspace has no installation, so it is not served.
    assert await store.get("T_TEST") is None


async def test_reinstall_replaces_the_tokens(repo: SqliteRepository) -> None:
    store = Installations(repo, "test-secret")
    await store.save(ALPHA)
    await store.revoke("T_ALPHA")
    again = Installation("T_ALPHA", "Alpha", "xoxb-again", "B_ALPHA", "U_OTHER", "xoxp-other")
    await store.save(again)
    assert await store.get("T_ALPHA") == again


def test_authorize_url_asks_for_bot_and_user_scopes() -> None:
    url = urlsplit(flow(FakeOAuth({})).authorize_url())
    query = {key: values[0] for key, values in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://slack.com/oauth/v2/authorize"
    assert query["client_id"] == "123.456"
    assert query["redirect_uri"] == "https://knappy.example/slack/oauth_redirect"
    assert query["scope"].split(",") == list(BOT_SCOPES)
    assert query["user_scope"].split(",") == list(USER_SCOPES)
    assert query["state"]


async def test_complete_exchanges_the_code() -> None:
    oauth = FakeOAuth(oauth_response())
    install = flow(oauth)
    state = parse_qs(urlsplit(install.authorize_url()).query)["state"][0]
    installation, app_id = await install.complete("the-code", state)
    assert installation == Installation("T_NEW", "New Co", "xoxb-new", "B_NEW", "U_INSTALLER", "xoxp-new")
    assert app_id == "A_KNAPPY"
    assert oauth.calls == [{
        "client_id": "123.456", "client_secret": "shh", "code": "the-code",
        "redirect_uri": "https://knappy.example/slack/oauth_redirect",
    }]


async def test_complete_refuses_stale_forged_and_enterprise_installs() -> None:
    oauth = FakeOAuth(oauth_response())
    state = parse_qs(urlsplit(flow(oauth).authorize_url()).query)["state"][0]
    with pytest.raises(InstallError, match="older than 10 minutes"):
        await flow(oauth, clock=lambda: NOW + timedelta(minutes=11)).complete("code", state)
    with pytest.raises(InstallError, match="older than 10 minutes"):
        await flow(oauth).complete("code", "forged")
    assert oauth.calls == []
    org = FakeOAuth(oauth_response(is_enterprise_install=True))
    with pytest.raises(InstallError, match="Enterprise Grid"):
        await flow(org).complete("code", state)


async def test_install_routes_end_to_end() -> None:
    installed: list[Installation] = []

    async def on_installed(installation: Installation) -> None:
        installed.append(installation)

    app = web.Application()
    add_install_routes(app, flow(FakeOAuth(oauth_response())), on_installed)
    async with TestClient(TestServer(app)) as client:
        start = await client.get("/slack/install", allow_redirects=False)
        assert start.status == 302
        location = urlsplit(start.headers["Location"])
        assert location.netloc == "slack.com"
        state = parse_qs(location.query)["state"][0]

        done = await client.get("/slack/oauth_redirect", params={"code": "the-code", "state": state})
        page = await done.text()
        assert done.status == 200
        assert "installed in New Co" in page
        assert "https://slack.com/app_redirect?app=A_KNAPPY&amp;team=T_NEW" in page
        assert [installation.team_id for installation in installed] == ["T_NEW"]

        refused = await client.get("/slack/oauth_redirect", params={"error": "access_denied"})
        assert refused.status == 400
        stale = await client.get("/slack/oauth_redirect", params={"code": "c", "state": "forged"})
        assert stale.status == 400
        assert len(installed) == 1


async def test_fleet_builds_each_workspace_once() -> None:
    found = {"T_ALPHA": ALPHA, "T_BETA": BETA}
    built: list[Installation] = []

    async def find(team_id: str) -> Installation | None:
        return found.get(team_id)

    async def build(installation: Installation) -> Any:
        built.append(installation)
        return object()

    fleet = Fleet(find, build)
    first = await fleet.get("T_ALPHA")
    assert await fleet.get("T_ALPHA") is first
    assert await fleet.get("T_UNKNOWN") is None
    assert await fleet.get(None) is None
    await fleet.get("T_BETA")
    assert len(fleet.all()) == 2
    replaced = await fleet.install(ALPHA)
    assert replaced is not first and await fleet.get("T_ALPHA") is replaced
    fleet.drop("T_BETA")
    assert fleet.all() == [replaced]
    assert built == [ALPHA, BETA, ALPHA]


def test_mcp_state_names_its_workspace() -> None:
    state = seal_state(derive_fernet("test-secret", "state"), PendingAuth("T_BETA", "U1", "google", "v"), NOW)
    assert state_workspace("test-secret", state, NOW) == "T_BETA"
    assert state_workspace("other-secret", state, NOW) is None
    assert state_workspace("test-secret", state, NOW + timedelta(hours=1)) is None


async def test_one_process_serves_every_installed_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_sdk.web.async_client import AsyncWebClient

    import knappy.main as main_module
    from knappy.db.factory import open_repository

    database = f"sqlite:///{tmp_path / 'knappy.db'}"
    seed = await open_repository(database)
    await seed.init_schema()
    await Installations(seed, "test-secret").save(ALPHA)
    await Installations(seed, "test-secret").save(BETA)
    await seed.close()

    for name, value in {
        "SLACK_APP_TOKEN": "xapp-test", "SLACK_SIGNING_SECRET": "secret", "GEMINI_API_KEY": "test-key",
        "KNAPPY_DATABASE_URL": database, "SLACK_CLIENT_ID": "123.456", "SLACK_CLIENT_SECRET": "shh",
        "KNAPPY_PUBLIC_URL": "https://knappy.example", "KNAPPY_SECRET_KEY": "test-secret",
        "KNAPPY_CALLBACK_PORT": "9124",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
    monkeypatch.setattr(main_module, "load_dotenv", lambda: None)

    async def auth_test(self, *args, **kwargs):
        return {"team_id": "T_?", "user_id": f"U_{self.token}"}

    async def idle(fleet):
        return None

    captured: dict[str, Any] = {}
    real_create_app = main_module.create_app

    def create_app(settings, **kwargs):
        captured.update(kwargs)
        return real_create_app(settings, **kwargs)

    served: list[set[str]] = []

    class Runner:
        async def cleanup(self) -> None:
            return None

    async def fake_serve(app, port):
        served.append({route.resource.canonical for route in app.router.routes()})
        return Runner()

    async def start_async(self):
        authorize = captured["authorize"]
        alpha = await authorize(enterprise_id=None, team_id="T_ALPHA")
        beta = await authorize(enterprise_id=None, team_id="T_BETA")
        assert (alpha.bot_token, alpha.user_token) == ("xoxb-alpha", "xoxp-alpha")
        assert (beta.bot_token, beta.user_token) == ("xoxb-beta", None)
        assert await authorize(enterprise_id=None, team_id="T_STRANGER") is None
        alpha_awareness = await captured["awareness"]("T_ALPHA")
        assert alpha_awareness is not None
        assert await captured["awareness"]("T_BETA") is None
        await captured["on_uninstalled"]("T_BETA")
        assert await authorize(enterprise_id=None, team_id="T_BETA") is None

    built: list = []
    real_runtime = main_module.KnappyRuntime

    def capture(*args, **kwargs):
        built.append(real_runtime(*args, **kwargs))
        return built[-1]

    monkeypatch.setattr(AsyncSocketModeHandler, "start_async", start_async)
    monkeypatch.setattr(AsyncWebClient, "auth_test", auth_test)
    monkeypatch.setattr(main_module, "_memory_loop", idle)
    monkeypatch.setattr(main_module, "_heartbeat_loop", idle)
    monkeypatch.setattr(main_module, "_awareness_loop", idle)
    monkeypatch.setattr(main_module, "create_app", create_app)
    monkeypatch.setattr(main_module, "serve", fake_serve)
    monkeypatch.setattr(main_module, "KnappyRuntime", capture)
    await main_module._serve()

    assert sorted(runtime.workspace_id for runtime in built) == ["T_ALPHA", "T_BETA"]
    alpha = next(runtime for runtime in built if runtime.workspace_id == "T_ALPHA")
    assert alpha.mcp is not None and alpha.mcp.workspace_id == "T_ALPHA"
    assert served == [{"/oauth/callback", "/slack/install", "/slack/oauth_redirect"}]

    reopened = await open_repository(database)
    assert await Installations(reopened, "test-secret").get("T_BETA") is None
    assert await Installations(reopened, "test-secret").get("T_ALPHA") == ALPHA
    await reopened.close()
