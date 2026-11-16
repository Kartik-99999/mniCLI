"""Named, reusable team configuration profiles."""

import json
import re

from omnicli import settings
from omnicli.paths import profiles_path

PROFILE_KEYS = tuple(settings.SCHEMA)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")


class ProfileError(ValueError):
    pass


def _name(name):
    name = str(name or "").strip()
    if not _NAME_RE.match(name):
        raise ProfileError("profile names must be 1-48 letters, numbers, dots, underscores, or hyphens")
    return name


def load_all():
    path = profiles_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        raise ProfileError("could not read profiles.json: {}".format(ex))
    if not isinstance(data, dict):
        raise ProfileError("profiles.json must contain a JSON object")
    return data


def _save_all(data):
    path = profiles_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def save(name):
    name = _name(name)
    data = load_all()
    data[name] = {key: settings.get(key) for key in PROFILE_KEYS}
    _save_all(data)
    return data[name]


def get(name):
    name = _name(name)
    data = load_all()
    profile = data.get(name)
    if not isinstance(profile, dict):
        raise ProfileError("profile '{}' was not found; run `omnicli profiles list`".format(name))
    return {key: value for key, value in profile.items() if key in settings.SCHEMA}


def apply(name):
    values = get(name)
    for key, value in values.items():
        if settings.SCHEMA[key][0] is dict:
            settings.unset(key)
            if isinstance(value, dict):
                for agent, setting in value.items():
                    settings.set_value("{}.{}".format(key, agent), setting)
        else:
            settings.set_value(key, value)
    return values


def delete(name):
    name = _name(name)
    data = load_all()
    if name not in data:
        return False
    del data[name]
    _save_all(data)
    return True
