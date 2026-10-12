"""Local browser-session helpers. This module never prints credential values."""

from html.parser import HTMLParser
from http.cookiejar import Cookie, CookieJar
import json
from pathlib import Path


DEFAULT_STATE = Path("~/.config/ydisk-saver/session.json")


class ScriptParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_script = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        self.in_script = tag == "script" and dict(attrs).get("id") == "preloaded-data"

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script:
            self.parts.append(data)


def cookie_jar(state_path: Path) -> CookieJar:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    jar = CookieJar()
    for item in state.get("cookies", []):
        domain = item.get("domain", "").lstrip(".")
        if domain != "yandex.ru" and not domain.endswith(".yandex.ru"):
            continue
        expires = item.get("expires", -1)
        jar.set_cookie(Cookie(
            version=0, name=item["name"], value=item["value"],
            port=None, port_specified=False, domain=domain,
            domain_specified=True, domain_initial_dot=True,
            path=item.get("path", "/"), path_specified=True,
            secure=item.get("secure", True),
            expires=None if expires == -1 else int(expires),
            discard=expires == -1,
            comment=None, comment_url=None, rest={}, rfc2109=False,
        ))
    return jar


def find_config(value):
    if isinstance(value, dict):
        if isinstance(value.get("sk"), str) and value.get("idClient"):
            return value
        for nested in value.values():
            result = find_config(nested)
            if result:
                return result
    elif isinstance(value, list):
        for nested in value:
            result = find_config(nested)
            if result:
                return result
    return None
