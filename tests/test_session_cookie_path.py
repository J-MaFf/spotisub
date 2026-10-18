"""Tests for the session cookie's path: a signed-in user can open a playlist
page even after an earlier signed-out visit to it (J-MaFf/homelab#830).

config.py sets APPLICATION_ROOT = "", and Flask used that as the session
cookie's Path. A browser stores a cookie with an empty Path under the request's
directory (RFC 6265 5.2.4). So the "Please log in" flash from a signed-out
visit to a playlist page left an anonymous session cookie scoped to that page.
Logging in at /login set a second cookie at "/". The browser sends the longer
path first, Flask reads the first one, and @login_required kept redirecting.

werkzeug 2.2's test client sends every cookie it holds to every path, so it
can't replay this. _Browser keeps cookies in http.cookiejar instead, which
scopes them by path and sends the longest path first, as browsers do.
"""
import http.cookiejar
import urllib.request

import pytest
from flask_login import UserMixin

from spotisub import spotisub as app
from spotisub import classes
from spotisub import database
from spotisub import routes
from spotisub.helpers import subsonic_helper

PLAYLIST_URL = ("/playlist/4fd64a4d4be349d18cd2e4a98d90b5f7"
                "/1/25/spotify_song.title/1/")


class _User(UserMixin):
    id = 1

    def check_password(self, password):
        return password == "secret"


class _UserQuery:
    """Stands in for User.query, which reads Flask-SQLAlchemy's database."""

    def filter_by(self, username):
        return self

    def first(self):
        return _User()

    def get(self, user_id):
        return _User() if user_id == 1 else None


class _UserModel:
    query = _UserQuery()


class _SetCookieHeaders:
    """Adapts a werkzeug response for CookieJar.extract_cookies()."""

    def __init__(self, headers):
        self._headers = headers

    def info(self):
        return self

    def get_all(self, name, default):
        return self._headers.getlist(name) or default


class _Browser:
    """A test client whose cookies are scoped and ordered like a browser's."""

    def __init__(self):
        self.client = app.test_client(use_cookies=False)
        self.jar = http.cookiejar.CookieJar()

    def _cookie_request(self, path):
        cookie_request = urllib.request.Request("http://localhost" + path)
        self.jar.add_cookie_header(cookie_request)
        return cookie_request

    def store(self, path, set_cookie):
        """Store a Set-Cookie header as if a response for `path` sent it."""
        response = app.response_class()
        response.headers.add("Set-Cookie", set_cookie)
        self.jar.extract_cookies(
            _SetCookieHeaders(response.headers), self._cookie_request(path))

    def open(self, path, method="GET", data=None):
        cookie_request = self._cookie_request(path)
        headers = {}
        if cookie_request.has_header("Cookie"):
            headers["Cookie"] = cookie_request.get_header("Cookie")
        response = self.client.open(
            path, method=method, data=data, headers=headers)
        self.jar.extract_cookies(
            _SetCookieHeaders(response.headers), cookie_request)
        return response

    def log_in(self):
        response = self.open(
            "/login", method="POST",
            data={"username": "joey", "password": "secret"})
        assert response.status_code == 302
        assert response.location.endswith("/overview/")


@pytest.fixture(autouse=True)
def fake_app_state(monkeypatch):
    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", False)
    monkeypatch.setattr(routes, "User", _UserModel)
    monkeypatch.setattr(classes, "User", _UserModel)
    monkeypatch.setattr(database, "user_exists", lambda: True)
    monkeypatch.setattr(
        subsonic_helper, "select_all_songs", lambda **kwargs: ([], 0))
    monkeypatch.setattr(
        subsonic_helper, "select_playlist_info_by_uuid", lambda *args: None)
    # Flask-Bootstrap is stubbed in tests, so its templates can't render.
    # Returning the template name also tells the playlist page apart from
    # the error page all_exception_handler renders.
    monkeypatch.setattr(
        routes, "render_template", lambda template, **kwargs: template)


def test_playlist_page_opens_after_a_signed_out_visit():
    browser = _Browser()

    # Signed out, for example because a restart changed SECRET_KEY.
    response = browser.open(PLAYLIST_URL)
    assert response.status_code == 302
    assert response.location.startswith("/login")

    browser.log_in()

    response = browser.open(PLAYLIST_URL)
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "playlist.html"


def test_playlist_page_ignores_an_old_anonymous_session_cookie():
    # What earlier versions left in browsers: an anonymous "session" cookie
    # with an empty Path, set while on the playlist page.
    anonymous = app.session_interface.get_signing_serializer(app).dumps(
        {"_flashes": [("message", "Please log in to access this page.")]})
    browser = _Browser()
    browser.store(PLAYLIST_URL, f"session={anonymous}; HttpOnly; Path=")

    browser.log_in()

    response = browser.open(PLAYLIST_URL)
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "playlist.html"
