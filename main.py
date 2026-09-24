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
from datetime import date, time, timedelta

# TO DO LIST
# HELP COMMAND
# ATTEMPT TO ADD A WAY TO PARSE THREAD NAME TO ADD TO THE LEADER BOARD LAST WORKSHEET DONE, AND MAYBE ADD A LIST OF NOT DONE WORKSHEETS THAT THEY CAN GET DM'D to them if needed
# streak freeze mechanic
# improve logging of threads by claim_thread()
# add phrase of the week = 5 points
# make non streak increasing submissions also send a message
# vip questions 5 points


# FIX LIST
# ATTACHING A PHOTO THAT UPDATES THE THREAD DOESNT COUNT AND DOESNT GIVE POINTS

# Loads the discord token and the firebase creds
load_dotenv()
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
text_points_emoji = "<:Linguazad_10:1495031679772004425>"
voice_points_emoji = "<:Linguazad_15:1495031741633794212>"
worksheet_points_emojis = {0: "<:Linguazad_20:1495031721329037384>",
                           1: "<:Linguazad_22:1495036190259150990>",
                           2: "<:Linguazad_24:1495036411018215536>",
                           3: "<:Linguazad_26:1495036335235272764>",
                           4: "<:Linguazad_28:1495036282521518290>"}
task_forum_ids = {"worksheet": 1495429343915016406,
                  "reactivation": 1495429369777098863,
                  "vocab": 1495429419920134184,
                  "retell": 1525353590900920440,
                  "connect": 1525353606599934073
                  }
weekly_bonuspercent = 10
min_worksheet_length = 100
min_dictation_length = 10
min_dictation_voice_length = 3
min_written_length = 20
min_speaking_length = 5

# Functions Section

def load_config(): #function for loading the config, used to create local var config
    config = {} #create an empty config dict
    default_config = { #fallback config if there is an empty key/whole db is empty
        "server_id" : None,
        "admin1" : None,
        "admin2" : None,
        "arabic_channel_id" : None,
        "franco_channel_id": None,
        "speaking_channel_id" : None,
        "dictation_channel_id" : None,
        "task_forum_id" : None,
        "leaderboard_channel_id": None,
        "leaderboard_message_id": None,
        "weekly_leaderboard_id": None,
        "log_channel_id" : None
    }
    raw_config = db.collection('config').document('settings').get()
    if raw_config.exists: #ensures that the config exists, then converts the snapshot to a dict
        data = raw_config.to_dict()
        for key in default_config.keys(): #convers the server id to int, and in the future any extra id
            if data.get(key) is not None: #.get not [key] to prevent crashing
                config[key] = int(data[key])
            else:  #if not found, set the key from the default config to the db, and load from local the default config for the key
                db.collection('config').document('settings').set({key:default_config[key]}, merge=True)
                config[key] = default_config[key]
        print("Config loaded successfully")
    else: #if the document doesn't exist, create it using the default config
        print(f"Failed to load config, loading default config")
        db.collection('config').document('settings').set(default_config)
        config = default_config.copy()
    return config

config = load_config()

async def check_user(message): #it checks that the message author is not the bot, or one of the admins, if not, it returns the user data as a dict
    if message.author == bot.user or message.author.id == config["admin1"] or message.author.id == config["admin2"]:
        return None

    doc_ref = db.collection('users').document(f'{str(message.author.id)}')
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
    user_data = db.collection('users').get()
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
            db.collection('config').document('settings').set({'leaderboard_message_id': str(msg.id)}, merge=True)
    else: #sends a new message incase there wasn't an old one on setup or if it was deleted
        msg = await channel.send(embed=embed)
        config['leaderboard_message_id'] = msg.id
        db.collection('config').document('settings').set({'leaderboard_message_id': str(msg.id)}, merge=True)
    return sorted_data

def missed_last_week(date_str): #function to check if 7 days have passed from the input date
    record_date = date.fromisoformat(date_str)
    return (date.today() - record_date).days > 7

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

# Points Helper Functions Section

def is_thread_claimed(channel_id: int) -> bool:
    return db.collection('thread_submissions').document(str(channel_id)).get().exists

async def claim_thread(message:discord.Message, tag: str):
    db.collection('thread_submissions').document(str(message.channel.id)).set({
        'channel_name': message.channel.name,
        'user_id': message.author.id,
        'user_name': message.author.name,
        'tag': tag,
        'awarded_at': str(date.today())
    })

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

async def handle_writing(message:discord.Message, user_data: dict, points:int = text_points, emoji:str = text_points_emoji, min_length: int = min_written_length, check_last_sent_time:bool = True, tag:str = None, alone:bool = True):
    """
    This helper function handles messages that qualify for writing points,
    it checks that the message is longer than the default minimum length of min_written_length,
    awards the default of text_points, and adds the text_points_emoji emoji as a default emoji.
    """

    if tag and is_thread_claimed(message.channel.id) and alone:
        await log(f"Follow up message sent in {message.channel.mention} by {message.author.mention}")
        return

    if user_data.get('last_writing_date') != str(date.today()) or not check_last_sent_time:
        if len(message.content) >= min_length:
            db.collection('users').document(str(message.author.id)).update({
                'points': firestore.Increment(points),
                'last_writing_date': str(date.today())
            })
            await message.add_reaction(emoji)
            sorted_data = await update_leaderboard()
            await send_points_message(message.author, points, "📝 Writing Completed", sorted_data)
            if tag:
                await claim_thread(message, tag)

            await log(
                f"Valid Message detected in {message.channel.mention} from {message.author.mention}, points awarded: {points}")

        elif message.attachments:
            for attachment in message.attachments:
                if attachment.content_type and attachment.content_type.startswith("image"):
                    db.collection('users').document(str(message.author.id)).update({
                        'points': firestore.Increment(points),
                        'last_writing_date': str(date.today())
                    })
                    await message.add_reaction(emoji)  # adds the text points emoji
                    sorted_data = await update_leaderboard()
                    await send_points_message(message.author, points, "🖼️ Image Submission Completed", sorted_data)
                    if tag:
                        await claim_thread(message, tag)
                    await log(f"Image detected in {message.channel.mention}, points awarded: {points}")
                    break
    else:
        await log(f"Message Detected in {message.channel.mention} from {message.author.mention}, but they already wrote one today. Points awarded: Zero")

async def handle_speaking(message:discord.Message, user_data: dict, points:int = voice_points, emoji:str = voice_points_emoji, min_length: int =min_speaking_length, check_last_sent_time: bool = True, tag:str = None, alone:bool = True):
    """
    This helper function handles messages that qualify for voice points,
    it checks that the message is longer than the default minimum length of min_speaking_length,
    awards a default of voice_points, and adds the voice_points_emoji emoji as a default emoji.
    """
    if tag and is_thread_claimed(message.channel.id) and alone:
        await log(f"Follow up message sent in {message.channel.mention} by {message.author.mention}")
        return

    if user_data.get('last_speaking_date') != str(date.today()) or not check_last_sent_time:
        for attachment in message.attachments:
            if attachment.is_voice_message() and attachment.duration >= min_length:  # checks if the user sent a voicenote, and if it is over 5 seconds
                db.collection('users').document(str(message.author.id)).update({
                    'points': firestore.Increment(points),
                    'last_speaking_date': str(date.today())
                })
                await message.add_reaction(emoji)  # adds the voice points emoji
                sorted_data = await update_leaderboard()
                await send_points_message(message.author, points, "🎤 Speaking Completed", sorted_data)
                if tag:
                    await claim_thread(message, tag)
                await log(
                    f"{message.author.mention} sent a voice message in {message.channel.mention}, points awarded: {points}")

            elif attachment.is_voice_message():
                await log(
                    f"{message.author.mention} sent a voice message in {message.channel.mention}, but it was shorter than {min_speaking_length}, points awarded: Zero")
    elif message:
        await log(
            f"{message.author.mention} sent a message in {message.channel.mention}, but they already sent one today, points awarded: Zero")

async def handle_worksheets(message:discord.Message, user_data: dict, points:int = worksheet_points, emoji:dict = None, tag:str = None):
    """

    """
    if emoji is None:
        emoji = worksheet_points_emojis

    if tag and is_thread_claimed(message.channel.id):
        await log(f"Follow up message sent in {message.channel.mention} by {message.author.mention}")
        return

    if len(message.content) < min_worksheet_length:
        await log(f"{message.author.mention} sent a message in {message.channel.mention}, but it was shorter than {min_worksheet_length}, points awarded: Zero")
        return

    effective_streak = min(user_data.get('streak'), 4)

    try:
        first_date = user_data.get('first_worksheet_thisWeek_date')
        effective_points = int(points * (1 + effective_streak * weekly_bonuspercent / 100))
        if missed_last_week(first_date):
            # new window, streak +1
            db.collection('users').document(str(message.author.id)).update({
                'points': firestore.Increment(effective_points),
                'last_worksheet_date': str(date.today()),
                'first_worksheet_thisWeek_date': str(date.today()),
                'streak': firestore.Increment(1)
            })
            await message.add_reaction(emoji[effective_streak])
            await log(
                f"{message.author.mention} sent a worksheet answer in {message.channel.mention}, points awarded: {effective_points}, streak: increased by 1")
        else:
            # within window, points with streak bonus but no streak increment
            db.collection('users').document(str(message.author.id)).update({
                'points': firestore.Increment(effective_points),
                'last_worksheet_date': str(date.today()),
            })

            await message.add_reaction(emoji[effective_streak])
            await log(
                f"{message.author.mention} sent a worksheet answer in {message.channel.mention}, points awarded: {effective_points}, streak: not increased because their last one was within 7 days ")
        sorted_data = await update_leaderboard()
        await send_points_message(message.author, effective_points, "🎉 Worksheet Completed", sorted_data, show_streak=True)
        if tag:
            await claim_thread(message, tag)

    except Exception as e:
        await log(f"[DEBUG] worksheet block crashed: {e}")

async def handle_dictation(message:discord.Message):
    """

    """
    if message.attachments and message.attachments[0].is_voice_message() and message.attachments[
        0].duration >= min_dictation_voice_length:
        db.collection('users').document(str(message.author.id)).update({
            'points': firestore.Increment(voice_points)
        })
        await message.add_reaction(voice_points_emoji)
        sorted_data = await update_leaderboard()
        await send_points_message(message.author, voice_points, "🎧 Voice Dictation Completed", sorted_data)
        await log(
            f"{message.author.mention} sent a voice message in {message.channel.mention} with over {min_dictation_voice_length} seconds of duration, points awarded: {voice_points}")

    if len(message.content) >= min_dictation_length:
        db.collection('users').document(str(message.author.id)).update({
            'points': firestore.Increment(text_points),
            'last_writing_date': str(date.today())
        })
        await message.add_reaction(text_points_emoji)
        sorted_data = await update_leaderboard()
        await send_points_message(message.author, text_points, "✍️ Written Dictation Completed", sorted_data)
        await log(
            f"{message.author.mention} sent a text message in {message.channel.mention} with over {min_dictation_length} chars, points awarded: {text_points}")

async def handle_message(message:discord.Message, user_data: dict, voice_points:int = voice_points, text_points:int = text_points, voice_emoji:str = voice_points_emoji, text_emoji:str = text_points_emoji, min_speaking_length: int =min_speaking_length, min_length:int = min_written_length, check_last_sent_time: bool = True, tag:str = None):
    if tag and is_thread_claimed(message.channel.id):
        await log(f"Follow up message sent in {message.channel.mention} by {message.author.mention}")
        return
    await handle_writing(message, user_data, points= text_points, emoji = text_emoji, min_length= min_written_length, alone = False, check_last_sent_time=check_last_sent_time, tag=tag)
    await handle_speaking(message, user_data, points= voice_points, emoji = voice_emoji, min_length= min_speaking_length, alone = False, check_last_sent_time=check_last_sent_time, tag=tag)

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
    monthly_leaderboard.start()
    weekly_leaderboard.start()
    check_streaks.start()

# Commands Section

@bot.command() #.setserver sets the server ID in the config, used elsewhere to instantly sync commands.
@commands.has_permissions(administrator=True)
async def setserver(ctx):
    try:
        server_id = str(ctx.guild.id)
        config_ref = db.collection('config').document('settings')
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
        config_data = db.collection('config').document('settings').get().to_dict()
        await interaction.response.send_message(config_data, ephemeral=True)
    except Exception as e:
        print(f"Error: {e}")

@bot.tree.command(name="configure", description="sets the admins and the channels", guild=get_guild()) # sets the settings document in the config collection in the DB
@discord.app_commands.checks.has_permissions(administrator=True)
async def configure(interaction: discord.Interaction, franco_channel: discord.TextChannel, arabic_channel: discord.TextChannel, speaking_channel : discord.TextChannel,
                    dictation_channel :discord.TextChannel, task_forum :discord.ForumChannel,leaderboard_channel: discord.TextChannel, weekly_leaderboard_channel: discord.TextChannel,
                    log_channel: discord.TextChannel, admin1: discord.Member, admin2: discord.Member):
    if config.get("server_id") is None:
        await interaction.response.send_message("Please set the server ID first by typing .setserver", ephemeral = True)
        return
    try:
        db.collection('config').document('settings').set({'franco_channel_id' : str(franco_channel.id), 'arabic_channel_id' : str(arabic_channel.id), "speaking_channel_id" : str(speaking_channel.id)
                                                             , "dictation_channel_id" : str(dictation_channel.id),"task_forum_id": str(task_forum.id),
                                                           'leaderboard_channel_id' : str(leaderboard_channel.id), 'weekly_leaderboard_id':str(weekly_leaderboard_channel.id),
                                                          'log_channel_id' : str(log_channel.id), 'admin1' : str(admin1.id), 'admin2' : str(admin2.id)},merge=True)
        config.update(load_config())
        await interaction.response.send_message("Config updated successfully!", ephemeral = True)
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
    db.collection('users').document(str(user.id)).update({
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
    db.collection('users').document(str(user.id)).update({
        'points': firestore.Increment(-points)
    })
    await interaction.response.send_message(f"{points} points removed from {user.mention}", ephemeral=True)
    await log(f"{points} points removed from {user.mention}")
    await update_leaderboard()

@bot.tree.command(name="set_streak", description="sets the streak for a certain user", guild=get_guild())
@discord.app_commands.checks.has_permissions(administrator=True)
async def set_streak(interaction: discord.Interaction, user: discord.Member, streak: int):
    try:
        db.collection('users').document(f'{str(user.id)}').set({'streak': streak}, merge=True)
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
    db.collection('users').document(f'{str(user.id)}').set({f'{date_to_reset}': "2000-01-01"}, merge = True)
    await interaction.response.send_message(f"{date.name} was reset for {user.mention}", ephemeral=True)
    await log(f"{date.name} was reset for {user.mention} by {interaction.user.mention}")


# Event handling part
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if isinstance(message.channel, discord.Thread) and message.channel.parent_id != config.get("task_forum_id"): #prevents it from reading messages in threads that are not in the task answers forum
        return

    # Check if the message is in a tracked channel
    tracked_channels = [
        config.get("franco_channel_id"),
        config.get("arabic_channel_id"),
        config.get("speaking_channel_id"),
        config.get("dictation_channel_id"),

    ]
    if (message.channel.id not in tracked_channels) and not (message.channel.parent_id == config.get("task_forum_id")):
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

    if message.channel.parent_id == config.get("task_forum_id"):
        if message.channel.applied_tags:
            in_tags = False
            for tag in message.channel.applied_tags:
                if tag.id in task_forum_ids.values():
                    in_tags = True
            if not in_tags:
                await log(f"{message.author.mention} sent a message in {message.channel.mention}, with a tag that is not in the tags list, the tags of the post are {message.channel.applied_tags}"
                          f"\nPlease update the bot with the proper tags")
            for tag in message.channel.applied_tags:
                if tag.id == task_forum_ids.get("worksheet"):
                    await handle_worksheets(message, user_data, tag = tag.name)
                if tag.id == task_forum_ids.get("reactivation"):
                    await handle_message(message, user_data,check_last_sent_time=False, tag = tag.name, voice_points= 20, text_points =20)
                if tag.id == task_forum_ids.get("vocab"):
                    await handle_message(message, user_data, check_last_sent_time=False, tag=tag.name, voice_points= 20, text_points =20)
                if tag.id == task_forum_ids.get("retell"):
                    await handle_message(message, user_data, check_last_sent_time=False, tag=tag.name, voice_points= 20, text_points =20)
                if tag.id == task_forum_ids.get("connect"):
                    await handle_message(message, user_data,check_last_sent_time=False, tag = tag.name, voice_points= 20, text_points =20)

    if message.channel.id == config['dictation_channel_id']:
        await handle_dictation(message)

    await bot.process_commands(message) #crucial so the bot can process written commands like .setserver

# Monthly Leaderboard Handling
@tasks.loop(time = time(hour = 0, minute = 0, second = 0))
async def monthly_leaderboard():
    if date.today().day != 1:
        return
    previous_month = date.today().replace(day=1) - timedelta(days=1)
    user_data = db.collection('users').get()
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

    winners:list = []
    for user in sorted_data[:3]:
        winner = await channel.guild.fetch_member(int(user['id']))
        winners.append(winner)

    winner_roles:list = [1552510875720745021, 1552510908335525918, 1552510967290667048]

    await channel.send(f"Congratulations to our winners!"
                       f"\n🥇 25-min private class with Sara {winners[0].mention}"
                       f"\n🥈 Speaking Club group class {winners[1].mention}"
                       f"\n🥉 Choose an upcoming story topic/or video reactivation topic {winners[2].mention}"
                       f"\n Message Sara for your Prize 🎖️🤩")
    for index, winner in enumerate(winners):
        role = channel.guild.get_role(winner_roles[index])
        await winner.add_roles(role)

    all_users = db.collection('users').get()
    for user in all_users:
        db.collection('users').document(user.id).update({'points': 0})
    await log(f"Monthly leaderboard for {previous_month.strftime('%B %Y')} sent, and all the points are reset! ")
    await update_leaderboard()

# Weekly Leaderboard Handling
@tasks.loop(time= time(hour = 0, minute = 0, second = 0))
#@bot.tree.command(name="weekly_leaderboard", description="Tests the weekly leaderboard", guild=get_guild())
async def weekly_leaderboard():
    if date.today().weekday() != 0 or date.today().day == 1:
        return
    user_data = db.collection('users').get()
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
@tasks.loop(time=time(hour=0, minute=0, second=0))
# @bot.tree.command(name="check_streaks", description="checks the streaks and resets if they haven't posted within a week", guild=get_guild())
async def check_streaks():
    all_users = db.collection('users').get()
    for user in all_users:
        user_data = user.to_dict()
        record_date = date.fromisoformat(user_data.get('last_worksheet_date', '2000-01-01'))
        days_from_last = (date.today() - record_date).days
        streak = user_data.get('streak', 0) #zero in the bracket is the fallback value
        member = bot.get_user(int(user.id))
        if days_from_last > 7 and streak > 0:
            db.collection('users').document(user.id).update({'streak': 0})
            await log(f"Reset streak for <@{user.id}>, their streak was {streak}")
            if member:
                try:
                    await member.send(f"Hey, sorry to say, but your worksheet streak of {streak} has been reset, please don't let this discourage you! you can start fresh whenever you have the time!"
                                  f"\nif you are having trouble with the worksheet and need some help, you can ask <@{config['admin1']}> or <@{config['admin2']}> for help at any time!")
                except discord.Forbidden:
                    await log(f"Could not sent streak reset message for {member.mention}, because they have their DMs closed")
            else:
                await log(f"could not send streak reminder for <@{user.id}>")
        elif days_from_last >= 5 and streak !=0:
            await log(f"sent streak reminder for <@{user.id}>")
            date_until_reset = (record_date + timedelta(days=7)).isoformat()
            if member:
                if days_from_last == 7:
                    msg = f"Hi, just wanted to remind you that today is your last day to keep your streak of {streak} alive, do a worksheet now to keep it going! \nYour streak expiry date is: {date_until_reset}"
                elif days_from_last == 6:
                    msg = f"Hello {member.mention}! I just wanted to remind you that your streak is {streak}, please do a worksheet when you can! \nYour streak expiry date is: {date_until_reset}"
                else:
                    msg = f"Hey, just a reminder that your streak of {streak} is going strong! Do a worksheet in the next 2 days to keep it alive! \nYour streak expiry date is: {date_until_reset}"
                try:
                    await member.send(msg)
                except discord.Forbidden:
                    await log(f"Could not sent streak reminder message for {member.mention}, because they have their DMs closed")
            else:
                await log(f"could not send streak reminder for <@{user.id}>")



webserver.keep_alive()
bot.run(token, log_handler=handler, log_level=logging.DEBUG)
