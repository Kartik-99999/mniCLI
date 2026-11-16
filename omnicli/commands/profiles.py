"""Manage named team profiles."""

import json

from omnicli import profiles as store

NAMES = ["profiles", "profile"]


def add_parser(subparsers):
    parser = subparsers.add_parser("profiles", aliases=["profile"], help="Save and apply reusable team settings")
    actions = parser.add_subparsers(dest="profile_action", required=True)
    actions.add_parser("list", help="List saved profiles")
    save = actions.add_parser("save", help="Save current team settings as a profile")
    save.add_argument("name")
    show = actions.add_parser("show", help="Show a saved profile")
    show.add_argument("name")
    use = actions.add_parser("use", help="Apply a profile to your default settings")
    use.add_argument("name")
    delete = actions.add_parser("delete", help="Delete a saved profile")
    delete.add_argument("name")
    parser.set_defaults(func=handler)


def handler(args):
    action = args.profile_action
    try:
        if action == "list":
            for name in sorted(store.load_all()):
                print(name)
            return 0
        if action == "save":
            store.save(args.name)
            print("Saved profile '{}'.".format(args.name))
            return 0
        if action == "show":
            print(json.dumps(store.get(args.name), indent=2, sort_keys=True))
            return 0
        if action == "use":
            values = store.apply(args.name)
            print("Applied profile '{}' ({} settings).".format(args.name, len(values)))
            return 0
        if action == "delete":
            if not store.delete(args.name):
                print("Profile '{}' was not found.".format(args.name))
                return 1
            print("Deleted profile '{}'.".format(args.name))
            return 0
    except store.ProfileError as ex:
        print("Profile error: {}".format(ex))
        return 2
    return 2
