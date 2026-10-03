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
# streak freeze mechanic
# improve logging of threads by claim_thread()
# add phrase of the week = 5 points
# make non streak increasing submissions also send a message


# FIX LIST
# ATTACHING A PHOTO THAT UPDATES THE THREAD DOESNT COUNT AND DOESNT GIVE POINTS

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
weekly_bonuspercent = 10
min_worksheet_length = 100
min_dictation_length = 10
min_dictation_voice_length = 3
min_written_length = 20
min_speaking_length = 5

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
        "task_forum_id" : None,
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
worksheet_points_emojis = config["worksheet_points_emojis"]
task_forum_ids = config["task_forum_ids"]
winner_roles = config["winner_roles"]

async def check_user(message): #it checks that the message author is not the bot, or one of the admins, if not, it returns the user data as a dict
    if message.author == bot.user or message.author.id == config["admin1"] or message.author.id == config["admin2"]:
        return None

    doc_ref = db.collection(users_collection).document(f'{str(message.author.id)}')
    doc = doc_ref.get()

    default_user = {
        'points': 0,
        'streak': 0,
        'last_worksheet_date': "2000-01-01",  # Placeholder dates
        'first_worksheet_thisWeek_date': "2000-01-01",
        'last_writing_date': "2000-01-01",
        'last_speaking_date': "2000-01-01"
    }

    if not doc.exists: #writes a new document with default values in case the author wasn't in the db
        doc_ref.set(default_user)
        return default_user
    user_data = doc.to_dict()
    needs_update = False

    for key, value in default_user.items():
        if key not in user_data:
            user_data[key] = value
            needs_update = True
    if needs_update:
        doc_ref.set(user_data, merge=True)
        await log(f"Healed document {message.author.mention}'s data from missing fields\nDocument ID: {message.author.id}")
    return user_data


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

def missed_last_week(date_str): #function to check if 7 days have passed from the input date
    record_date = date.fromisoformat(date_str)
    return (today() - record_date).days > 7

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

async def claim_thread(message:discord.Message, tag: str):
    db.collection(threads_collection).document(str(message.channel.id)).set({
        'channel_name': message.channel.name,
        'user_id': message.author.id,
        'user_name': message.author.name,
        'tag': tag,
        'awarded_at': str(today())
    })

def update_task_streak(message, user_data):
    if is_thread_claimed(message.channel.id):
        return
    # Existing date fields now track all task forum submissions.
    changes = {'last_worksheet_date': str(today())}
    if missed_last_week(user_data.get('first_worksheet_thisWeek_date')):
        changes['first_worksheet_thisWeek_date'] = str(today())
        changes['streak'] = firestore.Increment(1)
    db.collection(users_collection).document(str(message.author.id)).update(changes)

async def send_points_message(user, points, activity, sorted_data, show_streak=False):
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
            changes = {'points': firestore.Increment(points)}
            if not tag:
                changes['last_writing_date'] = str(today())
            db.collection(users_collection).document(str(message.author.id)).update(changes)
            if tag:
                update_task_streak(message, user_data)
                await claim_thread(message, tag)
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
                    changes = {'points': firestore.Increment(points)}
                    if not tag:
                        changes['last_writing_date'] = str(today())
                    db.collection(users_collection).document(str(message.author.id)).update(changes)
                    if tag:
                        update_task_streak(message, user_data)
                        await claim_thread(message, tag)
                    await message.add_reaction(emoji)  # adds the text points emoji
                    sorted_data = await update_leaderboard()
                    activity = f"🎉 {tag.title()} Completed" if tag else "🖼️ Image Submission Completed"
                    await send_points_message(message.author, points, activity, sorted_data, show_streak=bool(tag))
                    await log(f"Image detected in {message.channel.mention}, points awarded: {points}")
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
                changes = {'points': firestore.Increment(points)}
                if not tag:
                    changes['last_speaking_date'] = str(today())
                db.collection(users_collection).document(str(message.author.id)).update(changes)
                if tag:
                    update_task_streak(message, user_data)
                    await claim_thread(message, tag)
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

    effective_streak = min(user_data.get('streak'), 4)

    try:
        first_date = user_data.get('first_worksheet_thisWeek_date')
        effective_points = int(points * (1 + effective_streak * weekly_bonuspercent / 100))
        if missed_last_week(first_date):
            # new window, streak +1
            db.collection(users_collection).document(str(message.author.id)).update({
                'points': firestore.Increment(effective_points),
                'last_worksheet_date': str(today()),
                'first_worksheet_thisWeek_date': str(today()),
                'streak': firestore.Increment(1)
            })
            if tag:
                await claim_thread(message, tag)
            await message.add_reaction(emoji[effective_streak])
            await log(
                f"{message.author.mention} sent a worksheet answer in {message.channel.mention}, points awarded: {effective_points}, streak: increased by 1")
        else:
            # within window, points with streak bonus but no streak increment
            db.collection(users_collection).document(str(message.author.id)).update({
                'points': firestore.Increment(effective_points),
                'last_worksheet_date': str(today()),
            })
            if tag:
                await claim_thread(message, tag)
            await message.add_reaction(emoji[effective_streak])
            await log(
                f"{message.author.mention} sent a worksheet answer in {message.channel.mention}, points awarded: {effective_points}, streak: not increased because their last one was within 7 days ")
        sorted_data = await update_leaderboard()
        await send_points_message(message.author, effective_points, "🎉 Worksheet Completed", sorted_data, show_streak=True)
        return effective_points

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
    return vip_question_points

async def handle_message(message:discord.Message, user_data: dict, voice_points:int = voice_points, text_points:int = text_points,
                         voice_emoji:str = voice_points_emoji, text_emoji:str = text_points_emoji, min_speaking_length: int =min_speaking_length,
                         min_length:int = min_written_length, check_last_sent_time: bool = True, tag:str = None, admin_approved:bool = False):
    if tag and is_thread_claimed(message.channel.id):
        await log(f"Follow up message sent in {message.channel.mention} by {message.author.mention}")
        return 0

    awarded_points = 0
    if not (admin_approved and has_voice_message(message)): # only skips admin approved and has voice
        awarded_points += await handle_writing(message, user_data, points=text_points, emoji=text_emoji, min_length=min_length,
                             alone=False, check_last_sent_time=check_last_sent_time, tag=tag,
                             admin_approved=admin_approved)

    awarded_points += await handle_speaking(message, user_data, points= voice_points, emoji = voice_emoji, min_length= min_speaking_length,
                          alone = False, check_last_sent_time=check_last_sent_time, tag=tag, admin_approved = admin_approved)
    return awarded_points

async def handle_forum_task(message: discord.Message, user_data: dict, admin_approved: bool = False):
    """Route forum tags and return awarded points; approval keeps thread claims intact."""
    channel = message.channel
    if not isinstance(channel, discord.Thread) or channel.parent_id != config.get("task_forum_id"):
        raise ValueError("For Forum Task, choose a message in the configured task forum.")

    if is_thread_claimed(channel.id):
        await log(f"Follow up message sent in {channel.mention} by {message.author.mention}")
        return 0

    streak = min(user_data.get('streak', 0), 4)
    task_points = int(worksheet_points * (1 + streak * weekly_bonuspercent / 100))
    task_emoji = worksheet_points_emojis[streak]
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
                    dictation_channel :discord.TextChannel, vip_questions_channel: discord.TextChannel, task_forum :discord.ForumChannel,leaderboard_channel: discord.TextChannel, weekly_leaderboard_channel: discord.TextChannel,
                    log_channel: discord.TextChannel, admin1: discord.Member, admin2: discord.Member):
    if config.get("server_id") is None:
        await interaction.response.send_message("Please set the server ID first by typing .setserver", ephemeral = True)
        return
    try:
        db.collection(config_collection).document('settings').set({'franco_channel_id' : str(franco_channel.id), 'arabic_channel_id' : str(arabic_channel.id), "speaking_channel_id" : str(speaking_channel.id)
                                                             , "dictation_channel_id" : str(dictation_channel.id), 'vip_questions_channel_id': str(vip_questions_channel.id), "task_forum_id": str(task_forum.id),
                                                           'leaderboard_channel_id' : str(leaderboard_channel.id), 'weekly_leaderboard_id':str(weekly_leaderboard_channel.id),
                                                          'log_channel_id' : str(log_channel.id), 'admin1' : str(admin1.id), 'admin2' : str(admin2.id)},merge=True)
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

@bot.tree.command(name="set_streak", description="sets the streak for a certain user", guild=get_guild())
@discord.app_commands.checks.has_permissions(administrator=True)
async def set_streak(interaction: discord.Interaction, user: discord.Member, streak: int):
    try:
        db.collection(users_collection).document(f'{str(user.id)}').set({'streak': streak}, merge=True)
        await log(f"set streak for {user.mention} to {streak} by {interaction.user.mention}")
        await interaction.response.send_message(f"set streak for {user.mention} to {streak}", ephemeral=True)
        await update_leaderboard()
    except Exception as e:
        await log(f"Error setting streak for {user.name}: {e}")

@bot.tree.command(name="reset_date", description="resets a select date for a certain user", guild=get_guild())
@discord.app_commands.checks.has_permissions(administrator=True)
@app_commands.choices(date=[
    Choice(name="Date of the first worksheet sent this week", value="first_worksheet_thisWeek_date"),
    Choice(name="Date of the last worksheet sent", value="last_worksheet_date"),
    Choice(name=f"Date of the last voice note in the speaking channel", value="last_speaking_date"),
    Choice(name="Date of the last message sent in either writing channel", value="last_writing_date")
])
async def reset_date(interaction: discord.Interaction, user: discord.Member, date: Choice[str]):
    date_to_reset = date.value
    db.collection(users_collection).document(f'{str(user.id)}').set({f'{date_to_reset}': "2000-01-01"}, merge = True)
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

# daily check streaks
@tasks.loop(time=time(hour=0, minute=0, second=0, tzinfo=timezone.utc))
# @bot.tree.command(name="check_streaks", description="checks the streaks and resets if they haven't posted within a week", guild=get_guild())
async def check_streaks():
    all_users = db.collection(users_collection).get()
    for user in all_users:
        user_data = user.to_dict()
        record_date = date.fromisoformat(user_data.get('last_worksheet_date', '2000-01-01'))
        days_from_last = (today() - record_date).days
        streak = user_data.get('streak', 0) #zero in the bracket is the fallback value
        member = bot.get_user(int(user.id))
        if days_from_last > 7 and streak > 0:
            db.collection(users_collection).document(user.id).update({'streak': 0})
            await log(f"Reset streak for <@{user.id}>, their streak was {streak}")
            if member:
                try:
                    await member.send(f"Hey, sorry to say, but your task streak of {streak} has been reset. You can start fresh with any task forum activity!"
                                      f"\nIf you need help, you can ask <@{config['admin1']}> or <@{config['admin2']}> at any time!")
                except discord.Forbidden:
                    await log(f"Could not sent streak reset message for {member.mention}, because they have their DMs closed")
            else:
                await log(f"could not send streak reminder for <@{user.id}>")
        elif days_from_last >= 5 and streak !=0:
            await log(f"sent streak reminder for <@{user.id}>")
            date_until_reset = (record_date + timedelta(days=7)).isoformat()
            if member:
                if days_from_last == 7:
                    msg = f"Hi, just wanted to remind you that today is your last day to keep your streak of {streak} alive. Complete a task forum activity to keep it going! \nYour streak expiry date is: {date_until_reset}"
                elif days_from_last == 6:
                    msg = f"Hello {member.mention}! Your streak is {streak}. Complete a task forum activity when you can! \nYour streak expiry date is: {date_until_reset}"
                else:
                    msg = f"Hey, just a reminder that your streak of {streak} is going strong! Complete a task forum activity in the next 2 days to keep it alive! \nYour streak expiry date is: {date_until_reset}"
                try:
                    await member.send(msg)
                except discord.Forbidden:
                    await log(f"Could not sent streak reminder message for {member.mention}, because they have their DMs closed")
            else:
                await log(f"could not send streak reminder for <@{user.id}>")



webserver.keep_alive()
bot.run(token, log_handler=handler, log_level=logging.DEBUG)
