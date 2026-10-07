import pytest

from handshake.ring import RingAuthError
from handshake.tokens import OAUTH_URL, RefreshingToken, SecretToken, StaticToken, clean_token

from .fakes import response


@pytest.mark.parametrize("raw", [
    "eyJa.b.c",
    '  export TOKEN="eyJa.b.c"  ',
    "TOKEN='eyJa.b.c'",
    '{"access_token": "eyJa.b.c"}',
])
def test_clean_token_accepts_the_forms_people_paste(raw):
    assert clean_token(raw) == "eyJa.b.c"


def test_static_token_refuses_to_send_nothing():
    with pytest.raises(RingAuthError):
        StaticToken("")()


class FakeSecrets:
    def __init__(self, values):
        self.values = values
        self.reads = 0

    def get_secret_value(self, SecretId):
        self.reads += 1
        return {"SecretString": self.values[min(self.reads - 1, len(self.values) - 1)]}


def test_secret_token_rereads_after_its_ttl():
    now = [0.0]
    secrets = FakeSecrets(['export TOKEN="eyJ.one.x"', "eyJ.two.x"])
    token = SecretToken(secrets, "handshake/ring-token", ttl_s=60, clock=lambda: now[0])
    assert token() == "eyJ.one.x" and token() == "eyJ.one.x"
    assert secrets.reads == 1
    now[0] = 61
    assert token() == "eyJ.two.x"


def test_secret_token_with_an_empty_secret_is_an_auth_error():
    with pytest.raises(RingAuthError):
        SecretToken(FakeSecrets([""]), "s")()


class FakeOAuth:
    def __init__(self, replies):
        self.replies = replies
        self.posts = []

    def post(self, url, data=None, timeout=None):
        self.posts.append((url, data))
        return self.replies.pop(0)


def test_refresh_saves_the_new_refresh_token_and_caches_the_access_token():
    saved = {"refresh": "r1"}
    now = [1000.0]
    oauth = FakeOAuth([
        response(200, {"access_token": "a1", "refresh_token": "r2", "expires_in": 14400}),
        response(200, {"access_token": "a2", "refresh_token": "r3", "expires_in": 14400}),
    ])
    token = RefreshingToken("cid", "secret", lambda: saved["refresh"], lambda v: saved.update(refresh=v),
                            session=oauth, clock=lambda: now[0])
    assert token() == "a1" and token() == "a1"
    assert len(oauth.posts) == 1 and saved["refresh"] == "r2"
    assert oauth.posts[0][0] == OAUTH_URL and oauth.posts[0][1]["refresh_token"] == "r1"
    now[0] += 14400 - 60  # inside the early-refresh margin
    assert token() == "a2" and saved["refresh"] == "r3"
    assert oauth.posts[1][1]["refresh_token"] == "r2"


def test_a_rejected_refresh_is_an_auth_error():
    oauth = FakeOAuth([response(400, {"error": "invalid_grant"})])
    token = RefreshingToken("cid", "secret", lambda: "spent", lambda v: None, session=oauth)
    with pytest.raises(RingAuthError):
        token()
