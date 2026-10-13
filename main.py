# Imports, used later in bot
import discord
from discord.ext import commands
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import tasks
import logging
from dotenv import load_dotenv
import os
import json
import webserver
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlsplit

# TO DO LIST
# HELP COMMAND
# ATTEMPT TO ADD A WAY TO PARSE THREAD NAME TO ADD TO THE LEADER BOARD LAST WORKSHEET DONE, AND MAYBE ADD A LIST OF NOT DONE WORKSHEETS THAT THEY CAN GET DM'D to them if needed


# Loads the discord token and the firebase creds
load_dotenv()
environment = os.getenv("BOT_ENV", "dev")
if environment not in ("dev", "prod"):
    raise ValueError("BOT_ENV must be dev or prod")

# Keep existing collections for tests; production gets its own collections.
prefix = "prod_" if environment == "prod" else ""
users_collection = prefix + "users"
config_collection = prefix + "config"
threads_collection = prefix + "thread_submissions"

token = os.getenv("DISCORD_TOKEN")
firebase_creds_string = os.getenv("FIREBASE_CREDS")
if not all([token, firebase_creds_string]):
    print("Missing one or more environment variables")
    exit()

firebase_dict = json.loads(firebase_creds_string)
creds = credentials.Certificate(firebase_dict)
firebase_admin.initialize_app(creds)
db = firestore.client()

# Discord intents and logging handling
handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
intents = discord.Intents.default()
intents.presences = True
intents.message_content = True
intents.voice_states = True
intents.members = True
intents.reactions = True
bot = commands.Bot(command_prefix='.', intents=intents)

# Variables Section
text_points = 10
voice_points = 15
worksheet_points = 20
vip_question_points = 5
recommendation_points = 5
weekly_bonuspercent = 10
min_worksheet_length = 100
min_dictation_length = 10
min_dictation_voice_length = 3
min_written_length = 20
min_speaking_length = 5
phrase_of_the_week_points = 5

# Functions Section

def today():
    # Use GMT (UTC+0) for all activity dates, regardless of the host timezone.
    return datetime.now(timezone.utc).date()

def load_config():
    # These settings are individual Discord IDs; the new settings below use other types.
    default_config = {
        "server_id" : None,
        "admin1" : None,
        "admin2" : None,
        "arabic_channel_id" : None,
        "franco_channel_id": None,
        "speaking_channel_id" : None,
        "dictation_channel_id" : None,
        "vip_questions_channel_id": None,
        "recommendations_channel_id": None,
        "task_forum_id" : None,
        "phrase_of_the_week_channel_id": None,
        "leaderboard_channel_id": None,
        "leaderboard_message_id": None,
        "weekly_leaderboard_id": None,
        "log_channel_id" : None
    }
    raw_config = db.collection(config_collection).document('settings').get()
    if not raw_config.exists:
        raise ValueError(f"Missing {config_collection}/settings. Create the settings document in Firebase.")

    config = raw_config.to_dict()
    for key in default_config:
        config[key] = int(config[key]) if config.get(key) is not None else None

    required_settings = (
        "text_points_emoji", "voice_points_emoji", "vip_question_emoji",
        "worksheet_points_emojis", "task_forum_ids", "winner_roles"
    )
    for key in required_settings:
        if not config.get(key):
            raise ValueError(f"Missing {key} in {config_collection}/settings. Add this field in Firebase.")

    if not isinstance(config["worksheet_points_emojis"], list) or len(config["worksheet_points_emojis"]) != 5:
        raise ValueError("worksheet_points_emojis must be a list of five emojis, ordered from 20 to 28 points")
    if not isinstance(config["winner_roles"], list) or len(config["winner_roles"]) != 3:
        raise ValueError("winner_roles must be a list of three role IDs, ordered first, second, third")
    config["winner_roles"] = [int(role_id) for role_id in config["winner_roles"]]
    config["task_forum_ids"] = {
        name: int(config["task_forum_ids"][name])
        for name in ("worksheet", "reactivation", "vocab", "retell", "connect")
    }
    print(f"Config loaded for {environment}")
    return config

config = load_config()
text_points_emoji = config["text_points_emoji"]
voice_points_emoji = config["voice_points_emoji"]
vip_question_emoji = config["vip_question_emoji"]
phrase_of_the_week_emoji = config["vip_question_emoji"] # uses the same 5 point emojis
recommendation_emoji = config["vip_question_emoji"]
worksheet_points_emojis = config["worksheet_points_emojis"]
task_forum_ids = config["task_forum_ids"]
winner_roles = config["winner_roles"]

async def check_user(message): #it checks that the message author is not the bot, or one of the admins, if not, it returns the user data as a dict
    if message.author == bot.user or message.author.id == config["admin1"] or message.author.id == config["admin2"]:
        return None

    doc_ref = db.collection(users_collection).document(f'{str(message.author.id)}')
    return initialize_user(db.transaction(), doc_ref, today())


def missing_user_defaults(data):
    # Only add missing fields; never overwrite somebody's existing points.
    defaults = {
        'points': 0,
        'last_writing_date': "2000-01-01",
        'last_speaking_date': "2000-01-01",
    }
    missing = {}
    for field, value in defaults.items():
        if field not in data:
            missing[field] = value
    return missing


@firestore.transactional
def initialize_user(transaction, doc_ref, on_date):
    snapshot = doc_ref.get(transaction=transaction)
    data = snapshot.to_dict()
    if data is None:
        data = {}
    fields = missing_user_defaults(data)
    fields.update(normalize_streak(data, on_date))
    changes = {}
    for field, value in fields.items():
        if field not in data or data[field] != value:
            changes[field] = value
    if changes:
        transaction.set(doc_ref, changes, merge=True)
    data.update(fields)
    return data


async def update_leaderboard(): #function to update the leaderboard, returns sorted data
    user_data = db.collection(users_collection).get()
    docs = [{ 'id': doc.id, **doc.to_dict()} for doc in user_data] # added the discord ID (name of the document) to the user_data dict
    sorted_data = sorted(docs, key=lambda x: x['points'], reverse=True) #sorts the data by points, reverse to get descending order
    channel = bot.get_channel(config["leaderboard_channel_id"])
    embed = discord.Embed(title="🏆 Leaderboard", color=discord.Color.gold()) #creates an embed with only the title and colour
    embed.set_thumbnail(url="https://i.ibb.co/BKLCTWv5/4ab2bbcfa5b9a10891406d2a84e94004.webp") #sets a thumbnail to the embed
    embed.timestamp = discord.utils.utcnow() #adds a timestamp at the bottom of the embed
    for i, user in enumerate(sorted_data): #enumerate adds numbers to each item in the dict, used to display the rankings
        member = bot.get_user(int(user['id']))
        display_name = member.display_name if member else "Unknown User" # fallback if the member isn't the bot's memory for some reason
        embed.add_field( #adds field with each user to the existing embed
            name=f"#{i + 1} {display_name}",
            value=f"Points: {int(user['points'])} | Worksheet Streak: {int(user['streak'])}",
            inline=False
        )
    if config.get('leaderboard_message_id'): #checks if the message_id exists from before to update the message
        try:
            msg = channel.get_partial_message(config['leaderboard_message_id'])
            await msg.edit(embed=embed)
        except discord.NotFound: #if the message id isn't found (incorrect), it will send it again
            msg = await channel.send(embed=embed)
            config['leaderboard_message_id'] = msg.id
            db.collection(config_collection).document('settings').set({'leaderboard_message_id': str(msg.id)}, merge=True)
    else: #sends a new message incase there wasn't an old one on setup or if it was deleted
        msg = await channel.send(embed=embed)
        config['leaderboard_message_id'] = msg.id
        db.collection(config_collection).document('settings').set({'leaderboard_message_id': str(msg.id)}, merge=True)
    return sorted_data

# Weekly task streaks (UTC personal seven-day periods)
ANCHOR = "first_worksheet_thisWeek_date"
LAST_TASK = "last_worksheet_date"
STREAK_VERSION = 1


def empty_streak():
    return {
        "streak": 0,
        ANCHOR: None,
        LAST_TASK: None,
        "streak_shields": 0,
        "streak_last_processed_period": -1,
        "streak_last_reminder_key": None,
        "streak_version": STREAK_VERSION,
    }


def read_date(value):
    if not value or value == "2000-01-01":
        return None
    return date.fromisoformat(value)


def normalize_streak(data, on_date):
    """Migrate old records once, retaining their count and latest task date.

    The old moving earning-window date cannot reconstruct a historical anchor.
    Use the latest task as period zero; normal expiry still applies from that date.
    """
    state = empty_streak()
    if data.get("streak_version") == STREAK_VERSION:
        for field in state:
            if field in data:
                state[field] = data[field]
    elif data.get("streak_version") is not None:
        raise ValueError("Unsupported streak data version.")
    else:
        count = int(data.get("streak") or 0)
        if count > 0:
            last_task_date = read_date(data.get(LAST_TASK))
            if last_task_date is not None:
                state['streak'] = count
                state[ANCHOR] = last_task_date.isoformat()
                state[LAST_TASK] = last_task_date.isoformat()
    validate_streak(state, on_date)
    return state


def validate_streak(state, on_date):
    for field in ("streak", "streak_shields", "streak_last_processed_period"):
        if type(state[field]) is not int:
            raise ValueError(f"{field} must be a whole number.")
    if state["streak"] < 0:
        raise ValueError("Streak cannot be negative.")
    if state["streak_shields"] not in (0, 1):
        raise ValueError("Shields must be 0 or 1.")
    anchor_date = read_date(state[ANCHOR])
    last_task_date = read_date(state[LAST_TASK])
    if state["streak"] == 0:
        if anchor_date is not None or last_task_date is not None:
            raise ValueError("An inactive streak must have empty dates.")
        if state["streak_shields"] != 0 or state["streak_last_processed_period"] != -1:
            raise ValueError("An inactive streak must have empty dates, no shield, and processed period -1.")
        return
    if anchor_date is None or last_task_date is None:
        raise ValueError("An active streak needs an anchor date and latest task date.")
    if anchor_date > last_task_date:
        raise ValueError("The anchor cannot be after the latest task date.")
    if last_task_date > on_date:
        raise ValueError("The latest task date cannot be in the future (UTC).")
    # // 7 counts whole seven-day periods: 0 for days 1-7, 1 for days 8-14.
    current_period = (on_date - anchor_date).days // 7
    last_completed_period = (last_task_date - anchor_date).days // 7
    last_processed_period = state["streak_last_processed_period"]
    if last_processed_period >= current_period:
        raise ValueError("The current period is still open; it cannot be marked processed.")
    if last_processed_period < last_completed_period - 1:
        raise ValueError("Periods before the latest task's period must already be processed.")


def advance_streak(data, on_date):
    """Close elapsed periods exactly once, including after bot downtime."""
    state = normalize_streak(data, on_date)
    events = []
    if state["streak"] == 0:
        return state, events
    anchor_date = read_date(state[ANCHOR])
    last_task_date = read_date(state[LAST_TASK])
    current_period = (on_date - anchor_date).days // 7
    last_completed_period = (last_task_date - anchor_date).days // 7
    first_unchecked_period = state["streak_last_processed_period"] + 1
    # range excludes the current period: it is still open, so it cannot be missed.
    for period in range(first_unchecked_period, current_period):
        if period != last_completed_period:
            if state["streak_shields"] == 1:
                state["streak_shields"] = 0
                events.append("shield_used")
            else:
                events.append("streak_reset")
                return empty_streak(), events
        state["streak_last_processed_period"] = period
    return state, events


def record_task(data, on_date):
    """Award one weekly increase OR one shield, returning the pre-award streak.

    Resolve expiry before calculating the points bonus. The caller must ensure that
    this is a new qualifying task, and persist the result atomically with its claim.
    """
    state, events = advance_streak(data, on_date)
    bonus_streak = min(state["streak"], 4)
    if state["streak"] == 0:
        state[ANCHOR] = on_date.isoformat()
        state["streak"] = 1
        events.append("streak_started")
    else:
        anchor_date = read_date(state[ANCHOR])
        last_task_date = read_date(state[LAST_TASK])
        current_period = (on_date - anchor_date).days // 7
        last_completed_period = (last_task_date - anchor_date).days // 7
        if current_period != last_completed_period:
            state["streak"] += 1
            events.append("streak_increased")
        elif state["streak_shields"] == 0:
            state["streak_shields"] = 1
            events.append("shield_earned")
    state[LAST_TASK] = on_date.isoformat()
    return state, events, bonus_streak


def streak_reminder(state, on_date):
    """Return the final-two-days reminder for an unfinished current period."""
    if state["streak"] == 0:
        return None
    anchor_date = read_date(state[ANCHOR])
    last_task_date = read_date(state[LAST_TASK])
    elapsed_days = (on_date - anchor_date).days
    current_period = elapsed_days // 7
    day_in_period = elapsed_days % 7  # 0 is the first day; 5 and 6 are the last two.
    last_completed_period = (last_task_date - anchor_date).days // 7
    reminder_key = f"{current_period}:{day_in_period}"
    if last_completed_period == current_period:
        return None
    if day_in_period < 5:
        return None
    if state["streak_last_reminder_key"] == reminder_key:
        return None
    deadline = anchor_date + timedelta(days=current_period * 7 + 6)
    if state["streak_shields"] == 1:
        consequence = "Your shield will be used if you miss this period; your streak will stay the same."
    else:
        consequence = "Your streak will reset if you miss this period."
    return {
        "key": reminder_key,
        "message": (
            f"Your task streak is {state['streak']}. This period is still unfinished. "
            f"Complete a task by the end of {deadline.isoformat()} (UTC). {consequence}"
        ),
    }


def edit_streak_fields(data, on_date, changes):
    """Validate an admin repair as a single state change, without closing weeks."""
    allowed = {"streak", ANCHOR, LAST_TASK, "streak_shields", "streak_last_processed_period"}
    for field in changes:
        if field not in allowed:
            raise ValueError("Unknown streak field.")
    # An explicit zero is also a way to repair malformed historical state.
    if changes.get("streak") == 0:
        for field, value in changes.items():
            if field != "streak" and value not in (None, 0, -1):
                raise ValueError("Resetting a streak also clears its dates, shield, and processed period.")
        return empty_streak()
    # Apply supplied repairs before validation, so invalid saved values can be fixed.
    if data.get("streak_version") == STREAK_VERSION:
        state = empty_streak()
        for field in state:
            if field in data:
                state[field] = data[field]
    else:
        state = normalize_streak(data, on_date)
    if state["streak"] == 0 and changes.get("streak", 0) > 0:
        state[ANCHOR] = on_date.isoformat()
        state[LAST_TASK] = on_date.isoformat()
    state.update(changes)
    state["streak_last_reminder_key"] = None
    # Explicitly changing dates is an admin rebase: previous periods are settled.
    if ANCHOR in changes or LAST_TASK in changes:
        anchor = read_date(state[ANCHOR])
        if anchor and "streak_last_processed_period" not in changes:
            state["streak_last_processed_period"] = (on_date - anchor).days // 7 - 1
    validate_streak(state, on_date)
    return state


def get_guild(): #function to the get the guild ID, used in slash commands to sync quickly
    serverID =config.get("server_id")
    return discord.Object(id=serverID) if serverID else None

async def log(msg):
    print(msg)
    channel_id = config.get("log_channel_id")
    if not channel_id: #exits the function if log_channel isn't set up yet
        return
    logging_channel = bot.get_channel(channel_id)
    if logging_channel: #checks if the channel exists first, to prevent expectation spam
        try:
            await logging_channel.send(msg)
        except Exception as e:
            print(f"Failed to send log to Discord: {e}")

def has_voice_message(message: discord.Message) -> bool:
    return any(
        attachment.is_voice_message()
        for attachment in message.attachments
    )

async def fetch_submission_message(message_link: str) -> discord.Message:
    """Fetch the original message without running any commands contained in it."""
    link = urlsplit(message_link.strip().strip("<>"))
    parts = link.path.rstrip("/").split("/")
    discord_hosts = {
        "discord.com", "www.discord.com", "discordapp.com",
        "canary.discord.com", "ptb.discord.com",
    }
    if link.hostname not in discord_hosts or len(parts) != 5 or parts[1] != "channels":
        raise ValueError("Use Discord's Copy Message Link option and paste the full message link.")

    if not all(part.isdecimal() for part in parts[2:]):
        raise ValueError("The link must point to a message in a server.")

    channel_id = int(parts[3])
    message_id = int(parts[4])
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        raise ValueError("The link must point to a message in a text channel or forum thread.")
    return await channel.fetch_message(message_id)

# Points Helper Functions Section

def is_thread_claimed(channel_id: int) -> bool:
    return db.collection(threads_collection).document(str(channel_id)).get().exists


@firestore.transactional
def award_forum_task(transaction, user_ref, thread_ref, claim, reward_units, on_date, base_points=worksheet_points):
    # Read before writing. Firestore retries this function if another update
    # changes either document. Keep Discord messages outside this function.
    claimed = thread_ref.get(transaction=transaction)
    snapshot = user_ref.get(transaction=transaction)
    data = snapshot.to_dict()
    if data is None:
        data = {}
    if claimed.exists:
        return None

    state, events, bonus_streak = record_task(data, on_date)
    points_per_reward = int(base_points * (1 + bonus_streak * weekly_bonuspercent / 100))
    points = reward_units * points_per_reward
    changes = missing_user_defaults(data)
    changes.update(state)
    changes['points'] = firestore.Increment(points)
    # The points, streak, shield and thread claim either all save, or none do.
    transaction.set(user_ref, changes, merge=True)
    transaction.create(thread_ref, claim)
    return {'state': state, 'events': events, 'bonus_streak': bonus_streak, 'points': points}


def streak_event_text(events):
    messages = {
        'streak_started': "Your task streak has started!",
        'streak_increased': "This period is complete: your streak increased by 1.",
        'shield_earned': "You earned a shield! It protects one missed period (maximum 1).",
        'shield_used': "Your shield protected a missed period; that period earned no streak increase.",
        'streak_reset': "Your previous streak ended because a period was missed without a shield.",
    }
    lines = []
    for event_name in events:
        lines.append(messages[event_name])
    return "\n".join(lines)


async def send_points_message(user, points, activity, sorted_data, show_streak=False, streak_events=None):
    position = 0
    for index, row in enumerate(sorted_data):
        if row['id'] == str(user.id):
            position = index + 1
            total_points = row['points']
            streak = row.get('streak', 0)
            break
    if position == 0:
        await log(f"Could not find {user.mention} in the leaderboard after awarding points")
        return

    embed = discord.Embed(
        title=f"{activity}!",
        description=f"Ahlan **{user.display_name}**! Keep up the momentum!",
        color=0x288fcf
    )
    embed.set_thumbnail(url="https://i.ibb.co/BKLCTWv5/4ab2bbcfa5b9a10891406d2a84e94004.webp")
    if show_streak:
        embed.add_field(name="🔥 Current Streak", value=f"**{streak}** weeks", inline=True)
        embed.add_field(name="🛡️ Shield", value=f"**{row.get('streak_shields', 0)}/1**", inline=True)
        if streak_events:
            embed.add_field(name="Streak update", value=streak_event_text(streak_events), inline=False)
    embed.add_field(name="🪙 Points Earned", value=f"**+{points}**", inline=True)
    embed.add_field(name="📊 Leaderboard Rank", value=f"**#{position}**", inline=True)
    embed.add_field(name="🪙 Total Points", value=f"**{total_points}**", inline=True)
    embed.set_footer(text="Linguazad Community", icon_url="https://i.ibb.co/BKLCTWv5/4ab2bbcfa5b9a10891406d2a84e94004.webp")

    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        await log(f"Could not DM {user.mention} about their points. They might have DMs disabled.")

async def handle_writing(message:discord.Message, user_data: dict, points:int = text_points, emoji:str = text_points_emoji,
                         min_length: int = min_written_length, check_last_sent_time:bool = True, tag:str = None, alone:bool = True,
                         admin_approved:bool = False):
    """
    This helper function handles messages that qualify for writing points,
    it checks that the message is longer than the default minimum length of min_written_length,
    awards the default of text_points, and adds the text_points_emoji emoji as a default emoji.
    """

    if tag and is_thread_claimed(message.channel.id) and alone:
        await log(f"Follow up message sent in {message.channel.mention} by {message.author.mention}")
        return 0

    if user_data.get('last_writing_date') != str(today()) or not check_last_sent_time:
        if len(message.content) >= min_length or admin_approved:
            if tag:
                # handle_message checks both parts, then saves one forum reward.
                if not alone:
                    return points
                return await reward_forum_task(message, tag, 1)
            changes = {'points': firestore.Increment(points)}
            if not tag:
                changes['last_writing_date'] = str(today())
            db.collection(users_collection).document(str(message.author.id)).update(changes)
            await message.add_reaction(emoji)
            sorted_data = await update_leaderboard()
            activity = f"🎉 {tag.title()} Completed" if tag else "📝 Writing Completed"
            await send_points_message(message.author, points, activity, sorted_data, show_streak=bool(tag))
            await log(
                f"Valid Message detected in {message.channel.mention} from {message.author.mention}, points awarded: {points}")
            return points

        elif message.attachments:
            for attachment in message.attachments:
                if attachment.content_type and attachment.content_type.startswith("image"):
                    if tag:
                        # handle_message checks both parts, then saves one forum reward.
                        if not alone:
                            return points
                        return await reward_forum_task(message, tag, 1)
                    changes = {'points': firestore.Increment(points)}
                    if not tag:
                        changes['last_writing_date'] = str(today())
                    db.collection(users_collection).document(str(message.author.id)).update(changes)
                    await message.add_reaction(emoji)  # adds the text points emoji
                    sorted_data = await update_leaderboard()
                    activity = f"🎉 {tag.title()} Completed" if tag else "🖼️ Image Submission Completed"
                    await send_points_message(message.author, points, activity, sorted_data, show_streak=bool(tag))
                    await log(f"Image detected in {message.channel.mention} by {message.author.mention}, points awarded: {points}")
                    return points
    else:
        await log(f"Message Detected in {message.channel.mention} from {message.author.mention}, but they already wrote one today. Points awarded: Zero")
    return 0

async def handle_speaking(message:discord.Message, user_data: dict, points:int = voice_points, emoji:str = voice_points_emoji,
                          min_length: int =min_speaking_length, check_last_sent_time: bool = True, tag:str = None, alone:bool = True,
                          admin_approved:bool = False):
    """
    This helper function handles messages that qualify for voice points,
    it checks that the message is longer than the default minimum length of min_speaking_length,
    awards a default of voice_points, and adds the voice_points_emoji emoji as a default emoji.
    """
    if tag and is_thread_claimed(message.channel.id) and alone:
        await log(f"Follow up message sent in {message.channel.mention} by {message.author.mention}")
        return 0

    if user_data.get('last_speaking_date') != str(today()) or not check_last_sent_time:
        for attachment in message.attachments:
            if attachment.is_voice_message() and (admin_approved or attachment.duration >= min_length):
                if tag:
                    # handle_message checks both parts, then saves one forum reward.
                    if not alone:
                        return points
                    return await reward_forum_task(message, tag, 1)
                changes = {'points': firestore.Increment(points)}
                if not tag:
                    changes['last_speaking_date'] = str(today())
                db.collection(users_collection).document(str(message.author.id)).update(changes)
                await message.add_reaction(emoji)  # adds the voice points emoji
                sorted_data = await update_leaderboard()
                activity = f"🎉 {tag.title()} Completed" if tag else "🎤 Speaking Completed"
                await send_points_message(message.author, points, activity, sorted_data, show_streak=bool(tag))
                await log(
                    f"{message.author.mention} sent a voice message in {message.channel.mention}, points awarded: {points}")
                return points

            elif attachment.is_voice_message():
                await log(
                    f"{message.author.mention} sent a voice message in {message.channel.mention}, but it was shorter than {min_speaking_length}, points awarded: Zero")
    elif message:
        await log(
            f"{message.author.mention} sent a message in {message.channel.mention}, but they already sent one today, points awarded: Zero")
    return 0

async def handle_worksheets(message:discord.Message, user_data: dict, points:int = worksheet_points, emoji:list = None,
                            tag:str = None, admin_approved:bool = False):
    """

    """

    has_image = any(
        attachment.content_type
        and attachment.content_type.startswith("image/")
        for attachment in message.attachments
    )

    if emoji is None:
        emoji = worksheet_points_emojis

    if tag and is_thread_claimed(message.channel.id):
        await log(f"Follow up message sent in {message.channel.mention} by {message.author.mention}")
        return 0

    if len(message.content) < min_worksheet_length and not has_image and not admin_approved:
        await log(f"{message.author.mention} sent a message in {message.channel.mention}, but it was shorter than {min_worksheet_length}, points awarded: Zero")
        return 0

    try:
        # The checks above are unchanged. Save points, streak and claim together.
        return await reward_forum_task(message, tag, 1, points, emoji, "🎉 Worksheet Completed")

    except Exception as e:
        await log(f"[DEBUG] worksheet block crashed: {e}")
        raise

async def handle_dictation(message:discord.Message, admin_approved:bool = False):
    """

    """
    awarded_points = 0
    voice_attachment = None

    for attachment in message.attachments:
        if attachment.is_voice_message():
            voice_attachment = attachment
            break

    if voice_attachment and (
            admin_approved
            or voice_attachment.duration >= min_dictation_voice_length
    ):
        db.collection(users_collection).document(str(message.author.id)).update({
            'points': firestore.Increment(voice_points)
        })
        awarded_points += voice_points
        await message.add_reaction(voice_points_emoji)
        sorted_data = await update_leaderboard()
        await send_points_message(message.author, voice_points, "🎧 Voice Dictation Completed", sorted_data)
        await log(
            f"{message.author.mention} submitted voice dictation in {message.channel.mention}, points awarded: {voice_points}")

    if ((len(message.content) >= min_dictation_length or admin_approved)
            and not (admin_approved and has_voice_message(message))): # Skip writing rewards for admin-approved voice submissions.
        db.collection(users_collection).document(str(message.author.id)).update({
            'points': firestore.Increment(text_points),
            'last_writing_date': str(today())
        })
        awarded_points += text_points
        await message.add_reaction(text_points_emoji)
        sorted_data = await update_leaderboard()
        await send_points_message(message.author, text_points, "✍️ Written Dictation Completed", sorted_data)
        await log(
            f"{message.author.mention} submitted written dictation in {message.channel.mention}, points awarded: {text_points}")
    return awarded_points

async def handle_vip_question(message:discord.Message):
    db.collection(users_collection).document(str(message.author.id)).update({
        'points': firestore.Increment(vip_question_points)
    })
    await message.add_reaction(vip_question_emoji)
    await update_leaderboard()
    await log(f"{message.author.mention} submitted a vip question in {message.channel.mention}, points awarded: {vip_question_points}")
    return vip_question_points

async def handle_phrase_of_the_week(message:discord.Message):
    db.collection(users_collection).document(str(message.author.id)).update({
        'points': firestore.Increment(phrase_of_the_week_points)
    })
    await message.add_reaction(phrase_of_the_week_emoji)
    await update_leaderboard()
    await log(f"{message.author.mention} submitted a message in {message.channel.mention}, points awarded: {phrase_of_the_week_points}")
    return phrase_of_the_week_points


async def handle_recommendation(message:discord.Message):
    db.collection(users_collection).document(str(message.author.id)).update({
        'points': firestore.Increment(recommendation_points)
    })
    await message.add_reaction(recommendation_emoji)
    await update_leaderboard()
    await log(f"{message.author.mention} posted a recommendation in {message.channel.mention}, points awarded: {recommendation_points}")
    return recommendation_points


async def reward_forum_task(message, tag, reward_units, base_points=worksheet_points,
                            emoji=None, activity=None):
    """Save one task, even when both writing and speaking earned points."""
    if emoji is None:
        emoji = worksheet_points_emojis
    if activity is None:
        activity = f"🎉 {tag.title()} Completed"
    award_date = today()
    claim = {
        'channel_name': message.channel.name,
        'user_id': message.author.id,
        'user_name': message.author.name,
        'tag': tag,
        'awarded_at': str(award_date),
    }
    user_ref = db.collection(users_collection).document(str(message.author.id))
    thread_ref = db.collection(threads_collection).document(str(message.channel.id))
    result = award_forum_task(db.transaction(), user_ref, thread_ref, claim,
                              reward_units, award_date, base_points)
    if result is None:
        return 0
    await message.add_reaction(emoji[result['bonus_streak']])
    sorted_data = await update_leaderboard()
    await send_points_message(message.author, result['points'], activity, sorted_data,
                              show_streak=True, streak_events=result['events'])
    await log(f"{message.author.mention} sent a task in {message.channel.mention}, "
              f"points awarded: {result['points']}. {streak_event_text(result['events'])}")
    return result['points']


async def handle_message(message:discord.Message, user_data: dict, voice_points:int = voice_points, text_points:int = text_points,
                         voice_emoji:str = voice_points_emoji, text_emoji:str = text_points_emoji, min_speaking_length: int =min_speaking_length,
                         min_length:int = min_written_length, check_last_sent_time: bool = True, tag:str = None, admin_approved:bool = False):
    if tag and is_thread_claimed(message.channel.id):
        await log(f"Follow up message sent in {message.channel.mention} by {message.author.mention}")
        return 0

    awarded_points = 0
    reward_units = 0
    if not (admin_approved and has_voice_message(message)): # only skips admin approved and has voice
        writing_points = await handle_writing(message, user_data, points=text_points, emoji=text_emoji, min_length=min_length,
                             alone=False, check_last_sent_time=check_last_sent_time, tag=tag,
                             admin_approved=admin_approved)
        awarded_points += writing_points
        if writing_points > 0:
            reward_units += 1

    speaking_points = await handle_speaking(message, user_data, points= voice_points, emoji = voice_emoji, min_length= min_speaking_length,
                          alone = False, check_last_sent_time=check_last_sent_time, tag=tag, admin_approved = admin_approved)
    awarded_points += speaking_points
    if speaking_points > 0:
        reward_units += 1
    if tag and reward_units > 0:
        return await reward_forum_task(message, tag, reward_units)
    return awarded_points

async def handle_forum_task(message: discord.Message, user_data: dict, admin_approved: bool = False):
    """Route forum tags and return awarded points; approval keeps thread claims intact."""
    channel = message.channel
    if not isinstance(channel, discord.Thread) or channel.parent_id != config.get("task_forum_id"):
        raise ValueError("For Forum Task, choose a message in the configured task forum.")

    if is_thread_claimed(channel.id):
        await log(f"Follow up message sent in {channel.mention} by {message.author.mention}")
        return 0

    # The final bonus is calculated when saving, after checking for expiry.
    task_points = worksheet_points
    task_emoji = worksheet_points_emojis[0]
    recognized_tag = False

    for tag in channel.applied_tags:
        if tag.id not in task_forum_ids.values():
            continue
        recognized_tag = True
        if tag.id == task_forum_ids["worksheet"]:
            awarded_points = await handle_worksheets(
                message, user_data, tag=tag.name, admin_approved=admin_approved,
            )
        else:
            awarded_points = await handle_message(
                message, user_data, check_last_sent_time=False, tag=tag.name,
                voice_points=task_points, text_points=task_points,
                voice_emoji=task_emoji, text_emoji=task_emoji,
                admin_approved=admin_approved,
            )
        if awarded_points:
            return awarded_points

    if not recognized_tag:
        await log(f"No recognized task tag found in {channel.mention}; post tags: {channel.applied_tags}")
        if admin_approved:
            raise ValueError("This forum post needs a configured task tag so I can select its reward.")
    return 0

def start_background_tasks():
    # A fresh environment needs .setserver and /configure before reminders and rankings.
    required_settings = ("server_id", "leaderboard_channel_id", "weekly_leaderboard_id", "admin1", "admin2")
    if not all(config.get(key) for key in required_settings):
        print("Background tasks are waiting for .setserver and /configure.")
        return
    if not monthly_leaderboard.is_running():
        monthly_leaderboard.start()
    if not weekly_leaderboard.is_running():
        weekly_leaderboard.start()
    if not check_streaks.is_running():
        check_streaks.start()


@bot.event
async def on_ready(): # on ready event, essential for the bot, and has the loop checks such as the streaks reset and the monthly and weekly leaderboards
    print(f"✅ {bot.user} is online")
    if config["server_id"] is not None:
        try:
            guild = discord.Object(id = config.get("server_id"))
            synced = await bot.tree.sync(guild = guild)
            print(f"synced {len(synced)} commands to {config['server_id']}")
        except Exception as e:
            print(f"Error: {e}")
    else:
        print("Server ID not set, run .setserver to set the ID")
    start_background_tasks()

# Commands Section

@bot.command() #.setserver sets the server ID in the config, used elsewhere to instantly sync commands.
@commands.has_permissions(administrator=True)
async def setserver(ctx):
    try:
        server_id = str(ctx.guild.id)
        config_ref = db.collection(config_collection).document('settings')
        config_ref.set({'server_id': server_id}, merge=True)
        config["server_id"] = int(server_id)
        await ctx.author.send(f"✅ Server has been set. Commands will now sync to **{ctx.guild.name}**.")
        await log(f"✅ Server has been set. Commands will now sync to **{ctx.guild.name}**.")

    except Exception as e:
        await ctx.author.send("❌ Failed to set server ID.")
        await log(f"Error setting server ID: {e}")

@bot.tree.command(name="cfg", description="prints the config", guild=get_guild()) #/cfg prints the config, used mostly for debugging
@discord.app_commands.checks.has_permissions(administrator=True)
async def cfg(interaction):
    try:
        config_data = db.collection(config_collection).document('settings').get().to_dict()
        await interaction.response.send_message(config_data, ephemeral=True)
    except Exception as e:
        print(f"Error: {e}")

@bot.tree.command(name="configure", description="sets the admins and the channels", guild=get_guild()) # sets the settings document in the config collection in the DB
@discord.app_commands.checks.has_permissions(administrator=True)
async def configure(interaction: discord.Interaction, franco_channel: discord.TextChannel, arabic_channel: discord.TextChannel, speaking_channel : discord.TextChannel,
                    dictation_channel :discord.TextChannel, vip_questions_channel: discord.TextChannel,
                    task_forum :discord.ForumChannel,leaderboard_channel: discord.TextChannel,
                    weekly_leaderboard_channel: discord.TextChannel, phrase_of_the_week_channel: discord.TextChannel,
                    log_channel: discord.TextChannel, admin1: discord.Member, admin2: discord.Member,
                    recommendations_channel: discord.TextChannel = None):
    if config.get("server_id") is None:
        await interaction.response.send_message("Please set the server ID first by typing .setserver", ephemeral = True)
        return
    try:
        db.collection(config_collection).document('settings').set({
            'franco_channel_id' : str(franco_channel.id), 'arabic_channel_id' : str(arabic_channel.id), "speaking_channel_id" : str(speaking_channel.id),
            "dictation_channel_id" : str(dictation_channel.id), 'vip_questions_channel_id': str(vip_questions_channel.id),
            "task_forum_id": str(task_forum.id), 'phrase_of_the_week_channel_id':str(phrase_of_the_week_channel.id),
            'leaderboard_channel_id' : str(leaderboard_channel.id), 'weekly_leaderboard_id':str(weekly_leaderboard_channel.id),
            'log_channel_id' : str(log_channel.id), 'admin1' : str(admin1.id), 'admin2' : str(admin2.id)},merge=True)
        if recommendations_channel is not None:
            db.collection(config_collection).document('settings').set({
                'recommendations_channel_id': str(recommendations_channel.id)
            }, merge=True)
        config.update(load_config())
        await interaction.response.send_message("Config updated successfully!", ephemeral = True)
        start_background_tasks()
        await log(f"Server Settings updated successfully by {interaction.user.mention}")
    except Exception as e:
        print(f"Error: {e}")


@bot.tree.command(name="leaderboard", description="Updates the leaderboard", guild=get_guild()) #force updates the leaderboard
@discord.app_commands.checks.has_permissions(administrator=True)
async def leaderboard(interaction: discord.Interaction):
    await update_leaderboard()
    await interaction.response.send_message(
        f"Leaderboard successfully updated in <#{config['leaderboard_channel_id']}>", ephemeral=True)


@bot.tree.command(name="add_points", description="adds points to a user", guild=get_guild()) #command for adding points
@discord.app_commands.checks.has_permissions(administrator=True)
async def add_points(interaction: discord.Interaction, user: discord.User, points: int):
    db.collection(users_collection).document(str(user.id)).update({
        'points': firestore.Increment(points)
    })
    await interaction.response.send_message(f"{points} points added to {user.mention}", ephemeral=True)
    await log(f"{points} points added to {user.mention}")
    sorted_data = await update_leaderboard()
    if points > 0:
        await send_points_message(user, points, "🪙 Points Added", sorted_data)


@bot.tree.command(name="remove_points", description="removes points from a user", guild=get_guild()) #command for removing points
@discord.app_commands.checks.has_permissions(administrator=True)
async def remove_points(interaction: discord.Interaction, user: discord.User, points: int):
    db.collection(users_collection).document(str(user.id)).update({
        'points': firestore.Increment(-points)
    })
    await interaction.response.send_message(f"{points} points removed from {user.mention}", ephemeral=True)
    await log(f"{points} points removed from {user.mention}")
    await update_leaderboard()

@firestore.transactional
def save_streak_fields(transaction, user_ref, on_date, changes):
    snapshot = user_ref.get(transaction=transaction)
    data = snapshot.to_dict()
    if data is None:
        data = {}
    # Setting only the count must not accidentally revive already expired dates.
    if len(changes) == 1 and 'streak' in changes and changes['streak'] > 0:
        state, unused_events = advance_streak(data, on_date)
        data.update(state)
    state = edit_streak_fields(data, on_date, changes)
    fields = missing_user_defaults(data)
    fields.update(state)
    transaction.set(user_ref, fields, merge=True)
    return state


def format_streak_fields(state):
    lines = []
    for field in ('streak', ANCHOR, LAST_TASK, 'streak_shields', 'streak_last_processed_period'):
        lines.append(f"{field}: {state[field]}")
    return "\n".join(lines)


@bot.tree.command(name="streak_fields", description="Shows a member's current streak fields", guild=get_guild())
@discord.app_commands.checks.has_permissions(administrator=True)
async def streak_fields(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    try:
        user_ref = db.collection(users_collection).document(str(user.id))
        result = refresh_streak(db.transaction(), user_ref, today(), send_reminder=False)
        await interaction.followup.send(
            f"Streak fields for {user.mention} (UTC):\n{format_streak_fields(result['state'])}",
            ephemeral=True,
        )
    except ValueError as error:
        await interaction.followup.send(str(error), ephemeral=True)


@bot.tree.command(name="set_streak_field", description="Sets one streak field with validation", guild=get_guild())
@discord.app_commands.checks.has_permissions(administrator=True)
@app_commands.choices(field=[
    Choice(name="Streak count", value="streak"),
    Choice(name="Personal period anchor (YYYY-MM-DD)", value="first_worksheet_thisWeek_date"),
    Choice(name="Latest qualifying task (YYYY-MM-DD)", value="last_worksheet_date"),
    Choice(name="Shield balance (0 or 1)", value="streak_shields"),
    Choice(name="Last processed period (-1 before any close)", value="streak_last_processed_period"),
])
async def set_streak_field(interaction: discord.Interaction, user: discord.Member,
                           field: Choice[str], value: str):
    await interaction.response.defer(ephemeral=True)
    try:
        if field.value in (ANCHOR, LAST_TASK):
            parsed_value = date.fromisoformat(value).isoformat()
            if parsed_value != value:
                raise ValueError("Enter dates as YYYY-MM-DD.")
        else:
            parsed_value = int(value)
        user_ref = db.collection(users_collection).document(str(user.id))
        state = save_streak_fields(db.transaction(), user_ref, today(), {field.value: parsed_value})
        await interaction.followup.send(
            f"Updated {user.mention}:\n{format_streak_fields(state)}", ephemeral=True,
        )
        await log(f"{interaction.user.mention} set {field.value} to {parsed_value} for {user.mention}")
        await update_leaderboard()
    except ValueError as error:
        await interaction.followup.send(f"Cannot set that field: {error}", ephemeral=True)


@bot.tree.command(name="set_streak", description="sets the streak for a certain user", guild=get_guild())
@discord.app_commands.checks.has_permissions(administrator=True)
async def set_streak(interaction: discord.Interaction, user: discord.Member, streak: int):
    try:
        user_ref = db.collection(users_collection).document(str(user.id))
        save_streak_fields(db.transaction(), user_ref, today(), {'streak': streak})
        await log(f"set streak for {user.mention} to {streak} by {interaction.user.mention}")
        await interaction.response.send_message(f"set streak for {user.mention} to {streak}", ephemeral=True)
        await update_leaderboard()
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
    except Exception as e:
        await log(f"Error setting streak for {user.name}: {e}")

@bot.tree.command(name="reset_date", description="Resets a daily writing or speaking activity date", guild=get_guild())
@discord.app_commands.checks.has_permissions(administrator=True)
@app_commands.choices(date=[
    Choice(name="Date of the last voice note in the speaking channel", value="last_speaking_date"),
    Choice(name="Date of the last message sent in either writing channel", value="last_writing_date"),
])
async def reset_date(interaction: discord.Interaction, user: discord.Member, date: Choice[str]):
    # Streak dates must be edited together with the streak's bookkeeping.
    if date.value not in ('last_speaking_date', 'last_writing_date'):
        await interaction.response.send_message("Use /set_streak_field for streak dates.", ephemeral=True)
        return
    db.collection(users_collection).document(str(user.id)).set({date.value: "2000-01-01"}, merge=True)
    await interaction.response.send_message(f"{date.name} was reset for {user.mention}", ephemeral=True)
    await log(f"{date.name} was reset for {user.mention} by {interaction.user.mention}")


@bot.tree.command(name="award_submission", description="Award the submission of a certain user", guild=get_guild())
@discord.app_commands.checks.has_permissions(administrator=True)
@app_commands.choices(task_type=[
    Choice(name="Forum Task", value="forum_task"),
    Choice(name="Dictation", value="dictation"),
    Choice(name=f"Speaking", value="speaking"),
    Choice(name="Writing", value="writing"),
    Choice(name=f"VIP Question", value="VIP_question"),
    Choice(name=f"Phrase Of The Week", value="phrase_of_the_week"),
    Choice(name="Recommendation", value="recommendation"),
])
async def award_submission(interaction: discord.Interaction, message_link: str, task_type: Choice[str]):
    await interaction.response.defer(ephemeral=True)
    award_started = False
    try:
        message = await fetch_submission_message(message_link)
        if message.guild is None or interaction.guild is None or message.guild.id != interaction.guild.id:
            raise ValueError("Choose a message from the server where you ran this command.")

        user_data = await check_user(message)
        if user_data is None:
            await interaction.followup.send("This message's author is excluded from points.", ephemeral=True)
            return

        award_started = True
        if task_type.value == "forum_task":
            awarded_points = await handle_forum_task(message, user_data, admin_approved=True)
            skip_reason = "This forum thread has already been rewarded."
        elif task_type.value == "dictation":
            awarded_points = await handle_dictation(message, admin_approved=True)
            skip_reason = "No dictation reward was awarded."
        elif task_type.value == "speaking":
            awarded_points = await handle_speaking(message, user_data, admin_approved=True)
            skip_reason = (
                "This user has already received speaking points today."
                if has_voice_message(message) else "This message contains no Discord voice message."
            )
        elif task_type.value == "writing":
            awarded_points = await handle_writing(message, user_data, admin_approved=True)
            skip_reason = "This user has already received writing points today."
        elif task_type.value == "VIP_question":
            awarded_points = await handle_vip_question(message)
            skip_reason = "No VIP question reward was awarded."
        elif task_type.value == "phrase_of_the_week":
            awarded_points = await handle_phrase_of_the_week(message)
            skip_reason = "No Phrase Of The Week reward was awarded."
        elif task_type.value == "recommendation":
            awarded_points = await handle_recommendation(message)
            skip_reason = "No recommendation reward was awarded."

        else:
            raise ValueError("Choose one of the listed task types.")

        if awarded_points:
            await interaction.followup.send(
                f"Awarded **{awarded_points}** points to {message.author.mention} "
                f"for **{task_type.name}**.\n{message.jump_url}",
                ephemeral=True,
            )
            await log(
                f"{interaction.user.mention} approved {message.jump_url} as {task_type.name}; "
                f"{message.author.mention} received {awarded_points} points"
            )
        else:
            await interaction.followup.send(f"No points awarded. {skip_reason}", ephemeral=True)
    except ValueError as e:
        await interaction.followup.send(str(e), ephemeral=True)
    except discord.HTTPException as e:
        await log(f"award_submission Discord error: {e}")
        if award_started:
            reply = "Could not finish the reward's Discord updates. Check the user's points before retrying."
        elif isinstance(e, discord.NotFound):
            reply = "The message or channel could not be found."
        elif isinstance(e, discord.Forbidden):
            reply = "I don't have permission to read that message."
        else:
            reply = "Discord could not fetch that message. Please try again later."
        await interaction.followup.send(reply, ephemeral=True)
    except Exception as e:
        await log(f"award_submission error: {e}")
        reply = "Could not process this submission."
        if award_started:
            reply += " Check the user's points before retrying."
        await interaction.followup.send(reply, ephemeral=True)


# Event handling part
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    is_task_thread = isinstance(message.channel, discord.Thread) and message.channel.parent_id == config.get("task_forum_id")
    if isinstance(message.channel, discord.Thread) and not is_task_thread: #prevents it from reading messages in threads that are not in the task answers forum
        return

    # Check if the message is in a tracked channel
    tracked_channels = [
        config.get("franco_channel_id"),
        config.get("arabic_channel_id"),
        config.get("speaking_channel_id"),
        config.get("dictation_channel_id"),
        config.get("vip_questions_channel_id"),
        config.get("phrase_of_the_week_channel_id"),
        config.get("recommendations_channel_id"),
    ]
    if (message.channel.id not in tracked_channels) and not is_task_thread:
        await bot.process_commands(message)
        return

    try:
        user_data = await check_user(message)
    except Exception as e:
        print(f"check_user error: {e}")
        await bot.process_commands(message)
        return

    if user_data is None:
        await bot.process_commands(message)
        return

    if message.channel.id == config["franco_channel_id"] or message.channel.id == config["arabic_channel_id"]: #handles messages sent in the franco channel or the arabic channel
        await handle_writing(message, user_data)

    if message.channel.id == config["speaking_channel_id"] and message.attachments:
        await handle_speaking(message, user_data)

    if is_task_thread:
        await handle_forum_task(message, user_data)

    if message.channel.id == config['dictation_channel_id']:
        await handle_dictation(message)

    if message.channel.id == config['vip_questions_channel_id']:
        await handle_vip_question(message)

    if message.channel.id == config['phrase_of_the_week_channel_id']:
        await handle_phrase_of_the_week(message)

    if message.channel.id == config.get('recommendations_channel_id'):
        await handle_recommendation(message)

    await bot.process_commands(message) #crucial so the bot can process written commands like .setserver

# Monthly Leaderboard Handling
@tasks.loop(time = time(hour = 0, minute = 0, second = 0, tzinfo=timezone.utc))
async def monthly_leaderboard():
    if today().day != 1:
        return
    previous_month = today().replace(day=1) - timedelta(days=1)
    user_data = db.collection(users_collection).get()
    docs = [{ 'id': doc.id, **doc.to_dict()} for doc in user_data]
    sorted_data = sorted(docs, key=lambda x: x['points'], reverse=True)
    channel = bot.get_channel(config["weekly_leaderboard_id"])
    embed = discord.Embed(title=f"🏆 {previous_month.strftime('%B %Y')} Leaderboard", color=discord.Color.gold())
    embed.set_thumbnail(url="https://i.ibb.co/BKLCTWv5/4ab2bbcfa5b9a10891406d2a84e94004.webp")
    embed.timestamp = discord.utils.utcnow()

    for i, user in enumerate(sorted_data):
        member =  bot.get_user(int(user['id']))
        display_name = member.display_name if member else "Unknown User"  # fallback if the member isn't the bot's memory for some reason
        embed.add_field(
            name=f"#{i + 1} {display_name}",
            value=f"Points: {int(user.get('points') or 0)} | Worksheet Streak: {int(user.get('streak') or 0)}",
            inline=False
        )
    await channel.send(embed=embed)

    prizes = [
        "🥇 25-min private class with Sara",
        "🥈 Speaking Club group class",
        "🥉 Choose an upcoming story topic/or video reactivation topic"
    ]
    top_users = sorted_data[:3]

    if top_users:
        announcement = "Congratulations to our winners!"
        for index, user in enumerate(top_users):
            announcement += f"\n{prizes[index]} <@{user['id']}>"
        announcement += "\n Message Sara for your Prize 🎖️🤩"
        try:
            await channel.send(announcement)
        except discord.HTTPException as e:
            await log(f"Could not announce monthly winners: {e}")

    for index, user in enumerate(top_users):
        role = channel.guild.get_role(winner_roles[index])
        if role is None:
            await log(f"Monthly winner role {winner_roles[index]} was not found")
            continue
        try:
            winner = await channel.guild.fetch_member(int(user['id']))
            await winner.add_roles(role)
        except discord.HTTPException as e:
            # A missing member or failed role assignment must not block the reset.
            await log(f"Could not assign monthly winner role to <@{user['id']}>: {e}")

    all_users = db.collection(users_collection).get()
    for user in all_users:
        db.collection(users_collection).document(user.id).update({'points': 0})
    await log(f"Monthly leaderboard for {previous_month.strftime('%B %Y')} sent, and all the points are reset! ")
    await update_leaderboard()

# Weekly Leaderboard Handling
@tasks.loop(time= time(hour = 0, minute = 0, second = 0, tzinfo=timezone.utc))
#@bot.tree.command(name="weekly_leaderboard", description="Tests the weekly leaderboard", guild=get_guild())
async def weekly_leaderboard():
    if today().weekday() != 0 or today().day == 1:
        return
    user_data = db.collection(users_collection).get()
    docs = [{'id': doc.id, **doc.to_dict()} for doc in user_data]
    sorted_data = sorted(docs, key=lambda x: x['points'], reverse=True)
    channel = bot.get_channel(config["weekly_leaderboard_id"])
    embed = discord.Embed(title=f"🏆 Weekly Leaderboard", color=discord.Color.gold())
    embed.set_thumbnail(url="https://i.ibb.co/BKLCTWv5/4ab2bbcfa5b9a10891406d2a84e94004.webp")
    embed.timestamp = discord.utils.utcnow()
    for i, user in enumerate(sorted_data):
        member =  bot.get_user(int(user['id']))
        display_name = member.display_name if member else "Unknown User"  # fallback if the member isn't the bot's memory for some reason
        embed.add_field(
            name=f"#{i + 1} {display_name}",
            value=f"Points: {int(user['points'])} | Worksheet Streak: {int(user['streak'])}",
            inline=False
        )
    await channel.send(embed=embed)
    await log(f"Weekly Leaderboard sent")

@firestore.transactional
def refresh_streak(transaction, user_ref, on_date, send_reminder=True):
    snapshot = user_ref.get(transaction=transaction)
    data = snapshot.to_dict()
    if data is None:
        data = {}
    state, events = advance_streak(data, on_date)
    reminder = None
    if send_reminder:
        reminder = streak_reminder(state, on_date)
        if reminder:
            # Reserve this notice before sending, so retries/restarts cannot spam it.
            state['streak_last_reminder_key'] = reminder['key']
    fields = missing_user_defaults(data)
    fields.update(state)
    changes = {}
    for field, value in fields.items():
        if field not in data or data[field] != value:
            changes[field] = value
    if changes:
        transaction.set(user_ref, changes, merge=True)
    return {'state': state, 'events': events, 'reminder': reminder}


async def run_streak_checks():
    check_date = today()
    changed_streak = False
    for user in db.collection(users_collection).get():
        try:
            user_ref = db.collection(users_collection).document(user.id)
            result = refresh_streak(db.transaction(), user_ref, check_date)
            messages = []
            if result['events']:
                changed_streak = True
                messages.append(streak_event_text(result['events']))
            if result['reminder']:
                messages.append(result['reminder']['message'])
            if not messages:
                continue

            member = bot.get_user(int(user.id))
            if member is None:
                member = await bot.fetch_user(int(user.id))
            # A task might have arrived while fetching this Discord user.
            if result['reminder']:
                latest_state = normalize_streak(user_ref.get().to_dict(), check_date)
                if latest_state[LAST_TASK] != result['state'][LAST_TASK]:
                    messages.remove(result['reminder']['message'])
            if messages:
                await member.send("\n".join(messages))
            await log(f"Checked task streak for <@{user.id}>: " + " ".join(messages))
        except discord.HTTPException as error:
            await log(f"Could not deliver streak notice to <@{user.id}>: {error}")
        except Exception as error:
            # One damaged record or failed request must not stop all users' checks.
            await log(f"Could not check streak for <@{user.id}>: {error}")
    if changed_streak:
        await update_leaderboard()


@tasks.loop(time=time(hour=0, minute=0, second=0, tzinfo=timezone.utc))
async def check_streaks():
    await run_streak_checks()


@check_streaks.before_loop
async def prepare_streak_checks():
    await bot.wait_until_ready()
    # Reconcile missed boundaries after a restart, without waiting for midnight.
    await run_streak_checks()


webserver.keep_alive()
bot.run(token, log_handler=handler, log_level=logging.DEBUG)
