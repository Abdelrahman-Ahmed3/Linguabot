"""Run once to prepare dev and production settings in the existing Firestore database."""

import argparse
import json
import os

import firebase_admin
from dotenv import load_dotenv
from firebase_admin import credentials, firestore


# Existing test IDs from commit abea9a7 (the original main/origin/main).
# These are seed values only. The running bot reads its settings from Firebase.
DEV_SETTINGS = {
    "text_points_emoji": "<:Linguazad_10:1495031679772004425>",
    "voice_points_emoji": "<:Linguazad_15:1495031741633794212>",
    "vip_question_emoji": "<:Linguazad_5:1552531992053289041>",
    "worksheet_points_emojis": [
        "<:Linguazad_20:1495031721329037384>",
        "<:Linguazad_22:1495036190259150990>",
        "<:Linguazad_24:1495036411018215536>",
        "<:Linguazad_26:1495036335235272764>",
        "<:Linguazad_28:1495036282521518290>",
    ],
    "task_forum_ids": {
        "worksheet": 1495429343915016406,
        "reactivation": 1495429369777098863,
        "vocab": 1495429419920134184,
        "retell": 1525353590900920440,
        "connect": 1525353606599934073,
    },
    "winner_roles": [1552510875720745021, 1552510908335525918, 1552510967290667048],
}

# Production IDs supplied for the actual community server.
PROD_SETTINGS = {
    "text_points_emoji": "<:Linguazad_10:1555345094125817986>",
    "voice_points_emoji": "<:Linguazad_15:1555345202636525652>",
    "vip_question_emoji": "<:Linguazad_5:1555345125146755142>",
    "worksheet_points_emojis": [
        "<:Linguazad_20:1555345109166460938>",
        "<:Linguazad_22:1555345186794643536>",
        "<:Linguazad_24:1555345170692706366>",
        "<:Linguazad_26:1555345155718914159>",
        "<:Linguazad_28:1555345140388986911>",
    ],
    "task_forum_ids": {
        "worksheet": 1495475513571938455,
        "reactivation": 1497685620309626961,
        "vocab": 1497685639200772181,
        "retell": 1497685652748370073,
        "connect": 1497686128000766162,
    },
    "winner_roles": [1555345983649611867, 1555346065358725167, 1555346286671175811],
}

EMPTY_SETTINGS = {
    "server_id": None,
    "admin1": None,
    "admin2": None,
    "arabic_channel_id": None,
    "franco_channel_id": None,
    "speaking_channel_id": None,
    "dictation_channel_id": None,
    "vip_questions_channel_id": None,
    "task_forum_id": None,
    "leaderboard_channel_id": None,
    "leaderboard_message_id": None,
    "weekly_leaderboard_id": None,
    "log_channel_id": None,
}


def prepare_settings(db, dry_run=False):
    dev_ref = db.collection("config").document("settings")
    prod_ref = db.collection("prod_config").document("settings")
    existing_dev = dev_ref.get(retry=None, timeout=15).to_dict() or {}
    existing_prod = prod_ref.get(retry=None, timeout=15)

    # Add only absent settings; preserve existing channels, admins and custom settings.
    dev_defaults = {**EMPTY_SETTINGS, **DEV_SETTINGS}
    missing = {key: value for key, value in dev_defaults.items() if key not in existing_dev}
    if missing:
        if not dry_run:
            dev_ref.set(missing, merge=True, retry=None, timeout=15)
        print(f"{'Would add' if dry_run else 'Added'} {len(missing)} missing fields to config/settings.")
    else:
        print("Development settings are already prepared.")

    if existing_prod.exists:
        print("prod_config/settings already exists; left unchanged.")
    else:
        if not dry_run:
            prod_ref.create({**EMPTY_SETTINGS, **PROD_SETTINGS}, retry=None, timeout=15)
        print(f"{'Would create' if dry_run else 'Created'} prod_config/settings with production IDs.")

    print("User records and task claims were not changed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Show planned changes without writing them")
    args = parser.parse_args()

    load_dotenv()
    firebase_creds = os.getenv("FIREBASE_CREDS")
    if not firebase_creds:
        raise SystemExit("FIREBASE_CREDS is missing. Use the same credentials as the bot.")
    firebase_admin.initialize_app(credentials.Certificate(json.loads(firebase_creds)))
    prepare_settings(firestore.client(), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
