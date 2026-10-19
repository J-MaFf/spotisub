"""Spotipy helper"""
import os
import logging
import requests
import spotipy
import urllib3
from spotipy import SpotifyOAuth
from spotipy.util import Retry as SpotipyRetry
from spotisub import spotisub
from spotisub import constants
from spotisub.exceptions import SpotifyApiException


SP = None

# The longest Retry-After, in seconds, that a 429 is waited out for inside
# the request. Without a bound urllib3 sleeps out the whole Retry-After (up
# to Retry.DEFAULT_RETRY_AFTER_MAX, 6 h) and then re-sends the request with
# the Authorization header it was first built with, which has expired by
# then. Spotify answers a dev-mode app's exhausted /v1/tracks quota with a
# Retry-After of up to 24 h, so the cache thread slept 6 h at a time and
# woke to a 401 "Missing/invalid/expired access token". 30 s still covers
# Spotify's ordinary rolling 30-second rate limit.
MAX_RETRY_AFTER_SECONDS = 30


class BoundedRetry(SpotipyRetry):
    """spotipy's Retry, except that a retryable response (in practice a 429,
    or a 503) whose Retry-After is longer than MAX_RETRY_AFTER_SECONDS is
    handed straight back instead of slept on."""

    def increment(self, method=None, url=None, response=None, error=None,
                  _pool=None, _stacktrace=None):
        if response is not None and self._retry_after_too_long(response):
            raise urllib3.exceptions.MaxRetryError(
                _pool, url, urllib3.exceptions.ResponseError(
                    "Retry-After of " + str(response.headers.get("Retry-After"))
                    + " s is too long to wait out"))
        return super().increment(method, url, response=response, error=error,
                                 _pool=_pool, _stacktrace=_stacktrace)

    def _retry_after_too_long(self, response):
        try:
            retry_after = self.get_retry_after(response)
        except urllib3.exceptions.InvalidHeader:
            return False
        return retry_after is not None and retry_after > MAX_RETRY_AFTER_SECONDS


def build_requests_session():
    """The session spotipy would build for itself (same retry counts,
    methods, backoff and status codes), with BoundedRetry in place of
    spotipy's Retry. raise_on_status=False makes urllib3 return the last
    response once it stops retrying, so spotipy raises a SpotifyException
    that carries the real status and headers (Retry-After included) rather
    than a bare 429 "Max Retries".

    Unlike spotipy, read timeouts are retried too. spotipy leaves them off
    (read=False), so one slow response raised requests' ReadTimeout and
    killed the saved_tracks and user_playlists job threads. That is safe
    here because Spotisub only ever reads from api.spotify.com; the token
    refresh goes through SpotifyOAuth's own session, not this one."""
    retry = BoundedRetry(
        total=spotipy.Spotify.max_retries,
        connect=None,
        read=spotipy.Spotify.max_retries,
        allowed_methods=frozenset(['GET', 'POST', 'PUT', 'DELETE']),
        status=spotipy.Spotify.max_retries,
        backoff_factor=0.3,
        status_forcelist=spotipy.Spotify.default_retry_codes,
        raise_on_status=False)
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


def get_secrets():
    """Get Spotify api keys from env vars"""
    client_id = os.environ.get(
        constants.SPOTIPY_CLIENT_ID,
        constants.SPOTIPY_CLIENT_ID_DEFAULT_VALUE)
    client_secret = os.environ.get(
        constants.SPOTIPY_CLIENT_SECRET,
        constants.SPOTIPY_CLIENT_SECRET_DEFAULT_VALUE)
    redirect_uri = os.environ.get(
        constants.SPOTIPY_REDIRECT_URI,
        constants.SPOTIPY_REDIRECT_URI_DEFAULT_VALUE)

    if (client_id != ""
        and client_secret != ""
            and redirect_uri != ""):
        secrets = {}
        secrets["client_id"] = client_id
        secrets["client_secret"] = client_secret
        secrets["redirect_uri"] = redirect_uri
        return secrets
    raise SpotifyApiException()


def create_sp_client():
    """Creates the spotipy client"""
    secrets = get_secrets()
    scope = "user-top-read,user-library-read,user-read-recently-played,playlist-read-private"
    cache_path = os.path.abspath(os.curdir) + '/cache/spotipy_cache'
    
    try:
        # requests_timeout defaults to None (no timeout) on SpotifyOAuth --
        # unlike spotipy.Spotify below, which defaults to 5s. Since the
        # Spotify client above calls back into this OAuth object to
        # transparently refresh the access token roughly hourly, a network
        # hiccup during one of those refreshes hangs the whole client
        # forever instead of raising. Confirmed live: a reimport-all run
        # died silently mid-way with no error logged, stuck for hours;
        # /proc/<pid>/net/tcp showed sockets in CLOSE_WAIT (remote closed,
        # local side never noticed). Pin an explicit timeout so a stalled
        # refresh fails fast and can be retried instead of hanging.
        creds = SpotifyOAuth(
            scope=scope,
            client_id=secrets["client_id"],
            client_secret=secrets["client_secret"],
            redirect_uri=secrets["redirect_uri"],
            open_browser=False,
            cache_path=cache_path,
            requests_timeout=10)

        # Same 10 s read budget as the OAuth refresh above; spotipy's
        # default is 5 s.
        return spotipy.Spotify(
            auth_manager=creds,
            requests_session=build_requests_session(),
            requests_timeout=10)
    except EOFError as e:
        logging.error(
            "Failed to authenticate with Spotify in non-interactive environment. "
            "Please ensure a valid cached token exists at: %s", cache_path)
        logging.error(
            "To fix this, run Spotisub interactively once to obtain a valid token, "
            "or ensure the SPOTIPY_CACHE file is properly set up.")
        raise SpotifyApiException(
            "Spotify authentication failed in non-interactive mode. "
            "Please run Spotisub interactively first to cache a valid token.") from e


def get_spotipy_client():
    """Get the previously created spotipy client"""
    return SP


SP = create_sp_client()
