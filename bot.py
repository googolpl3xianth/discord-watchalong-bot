import discord
from discord import app_commands
from discord.ext import tasks, commands
import os
from dotenv import load_dotenv
from db import MyBot, RoleRequest, RoleClass
import emoji
import datetime
from utils import parse_schedule, get_available_emoji
import zoneinfo
import datetime as dt

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
TICKET_CHANNEL_ID = int(os.getenv("TICKET_CHANNEL_ID"))
ROLE_CHANNEL_ID = int(os.getenv("ROLE_CHANNEL_ID"))
PING_CHANNEL_ID = int(os.getenv("PING_CHANNEL_ID"))

intents = discord.Intents.default()
intents.message_content = True

bot = MyBot(command_prefix="$", intents=intents)

ping_tracker = {}
day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

@tasks.loop(minutes=1)
async def weekly_ping_task():
    await bot.wait_until_ready()
    
    now = datetime.datetime.now(zoneinfo.ZoneInfo("America/Los_Angeles"))
    current_day = now.weekday()
    ping_channel = bot.get_channel(PING_CHANNEL_ID) 
    if not ping_channel:
        print(f"[ERROR] Ping channel not found at: {ping_channel}")
        return

    for role_name, role_data in bot.data.roles.items():
        if role_data.ping_day == current_day:
            target_time = datetime.time.fromisoformat(role_data.ping_time)
            if target_time and now.hour == target_time.hour and now.minute == target_time.minute:
                today_str = now.strftime("%Y-%m-%d")
                if ping_tracker.get(role_name) != today_str:
                    role = ping_channel.guild.get_role(role_data.role_id)
                    if role:
                        await ping_channel.send(f"{role.mention} It is time for the **{role_name}** weekly watchalong!")
                    ping_tracker[role_name] = today_str

@bot.event
async def on_ready():
    print("Bot online")
    if bot.react_message_id is None:
        await init_react_message()
    await update_role_message()

    if not weekly_ping_task.is_running():
        weekly_ping_task.start()
        print("Weekly ping loop started!")

@bot.tree.command(name="rq", description="Request a new anime watchalong role")
@app_commands.describe(
    role_name="The name of the role",
    day="Day of week for ping (e.g., mon, tue in America/Los_Angeles)",
    time="Time for ping (e.g., 14:30, 2:30 PM in America/Los_Angeles)",
    react_emoji="Emoji for the reaction"
)
async def request_role(interaction: discord.Interaction, # Replaces ctx
    role_name: str, 
    day: str = None,
    time: str = None, 
    react_emoji: str = None
):
    await interaction.response.defer()

    user = interaction.user
    channel = bot.get_channel(TICKET_CHANNEL_ID)

    if not channel:
        await interaction.followup.send("Failure to find channel", ephemeral=True)
        return
    if(role_name in bot.data.roles):
        await interaction.followup.send(f"Role {role_name} already exists: {bot.data.roles[role_name]}")
        return
    existing_role = discord.utils.get(interaction.guild.roles, name=role_name)
    if existing_role:
        await interaction.followup.send(f"❌ A role named `{role_name}` already exists in this server", ephemeral=True)
        return
    if len(bot.data.roles) >= 20:
        await interaction.followup.send("❌ The role menu is full! (Discord limits messages to 20 reactions). Please request an admin to remove old roles first.", ephemeral=True)
        return
    role_name = role_name.strip()
    if len(role_name) > 100:
        await interaction.followup.send("❌ Role names cannot be longer than 100 characters.", ephemeral=True)
        return
    
    day_int, parsed_time = parse_schedule(day, time)
    if(not react_emoji):
        react_emoji = get_available_emoji(bot)
    elif(not emoji.is_emoji(react_emoji)):
        await interaction.followup.send(f"Is not valid emoji")

    if day_int == -1:
        await interaction.followup.send(f"❌ I didn't understand the day `{day}`. Please use abbreviations like 'Mon', 'Tue', etc.")
        return
    if parsed_time == "err":
        await interaction.followup.send(f"❌ I didn't understand the time `{time}`. Try formats like `14:30` or `2:30 PM`.")
        return
    
    day_str = "n/a"
    time_str = "n/a"
    if day_int is not None:
        global day_names
        day_str = day_names[day_int]
    if parsed_time:
        time_str = parsed_time.strftime("%I:%M %p")

    bot.data.role_queue[role_name] = RoleRequest(
        requester_id=user.id,
        ping_day=day_int,
        ping_time=parsed_time.isoformat() if parsed_time else None,
        emoji=react_emoji
    )
    bot.save_data()

    await interaction.followup.send(f"Successfully requested the role **{role_name}**!")

    await channel.send(
        f"**New Role Request from {user.name}**\n"
        f"**Role:** `{role_name}`\n"
        f"**Weekly Ping:** Every {day_str} at {time_str} in America/Los_Angeles\n\n"
        f"Admins: Use `/addq {role_name}` or `/rmq {role_name}` to accept or deny."
    )

@bot.tree.command(name="addq", description="Approve a role from the queue")
@app_commands.describe(
    role_name="The name of the role, Leave blank to approve the most recent request",
    day="Overwrites requet's day of week for ping (e.g., mon, tue in America/Los_Angeles)",
    time="Overwrites requet's time for ping (e.g., 14:30, 2:30 PM in America/Los_Angeles)",
    react_emoji="Overwrites requet's emoji for the reaction"
)
@app_commands.default_permissions(manage_roles=True)
async def addq(
    interaction: discord.Interaction, 
    role_name: str = None, 
    day: str = None, 
    time: str = None, 
    react_emoji: str = None
):
    await interaction.response.defer()
    if(not role_name and bot.data.role_queue):
        role_name = list(bot.data.role_queue.keys())[-1]
    else:
        await interaction.followup.send("Queue is empty", ephemeral=True)
        return
    
    if role_name not in bot.data.role_queue:
        await interaction.followup.send(f"No request by that name. Queue:\n{list(bot.data.role_queue.keys())}")
        return

    if role_name in bot.data.roles:
        await interaction.followup.send(f"Role `{role_name}` already exists!")
        return
    
    global day_names

    request_data = bot.data.role_queue[role_name]
    if day or time:
        day_to_parse = day if day else day_names[request_data.ping_day]
        time_to_parse = time if time else datetime.time.fromisoformat(request_data.ping_time).strftime("%H:%M")
        
        day_int, parsed_time = parse_schedule(day_to_parse, time_to_parse)
        
        if day_int is not None:
            request_data.ping_day = day_int
        if parsed_time is not None:
            request_data.ping_time = parsed_time.isoformat()
    
    if react_emoji:
        request_data.emoji = react_emoji
    
    perms = discord.Permissions(send_messages=True, read_messages=True)
    role = await interaction.guild.create_role(
        name=role_name, 
        colour=discord.Colour.blue(), 
        permissions=perms,
        mentionable=True,
        hoist=False
    )
    bot.data.roles[role_name] = RoleClass(
        role_id = role.id,
        ping_day = request_data.ping_day,
        ping_time = request_data.ping_time
    )

    bot.data.reaction_map[request_data.emoji] = role.id

    ping = False
    if(request_data.ping_day and request_data.ping_time):
        ping = True
        day_str = day_names[request_data.ping_day]
        dt_obj = datetime.time.fromisoformat(request_data.ping_time)
        time_str = dt_obj.strftime("%I:%M %p")
    del bot.data.role_queue[role_name]
    await update_role_message()
    bot.save_data()

    message = (
        f"**Created New Role from <@{request_data.requester_id}>**\n"
        f"**Role:** `{role_name}`\n"
    )
    if ping:
        message += f"**Weekly Ping:** Every {day_str} at {time_str} in America/Los_Angeles\n\n"
    else:
        message += f"**No Weekly Ping Set**\n\n"

    await interaction.followup.send(message)


@bot.tree.command(name="rmq", description="Remove request from queue")
@app_commands.describe(role_name="Leave blank to deny the most recent request")
@app_commands.default_permissions(manage_roles=True)
async def rmq(interaction: discord.Interaction, role_name: str = None, ):
    await interaction.response.defer()
    if(not role_name and bot.data.role_queue):
        role_name = list(bot.data.role_queue.keys())[-1]
    if(role_name in bot.data.role_queue):
        del bot.data.role_queue[role_name]
        bot.save_data()
        await interaction.followup.send(f"Successfully removed {role_name}")
    else:
        await interaction.followup.send(f"No request by that name, list:\n{bot.data.role_queue}")

@bot.tree.command(name="listq", description="Displays request queue")
@app_commands.default_permissions(manage_roles=True)
async def listq(interaction: discord.Interaction):
    await interaction.response.defer()
    await interaction.followup.send(f"list queue:\n{bot.data.role_queue}")

@bot.tree.command(name="add", description="Adds a role, bypassing the queue")
@app_commands.describe(
    role_name="The name of the role",
    day="Day of week for ping (e.g., mon, tue in America/Los_Angeles)",
    time="Time for ping (e.g., 14:30, 2:30 PM in America/Los_Angeles)",
    react_emoji="Emoji for the reaction"
)
@app_commands.default_permissions(manage_roles=True)
async def add(
    interaction: discord.Interaction, 
    role_name: str, 
    day: str = None, 
    time: str = None, 
    react_emoji: str = None
):
    await interaction.response.defer()
    if not role_name:
        await interaction.followup.send("No role name detected", ephemeral=True)
        return
    if(role_name in bot.data.roles):
        await interaction.followup.send(f"Role {role_name} already exists: {bot.data.roles[role_name]}")
        return
    existing_role = discord.utils.get(interaction.guild.roles, name=role_name)
    if existing_role:
        await interaction.followup.send(f"❌ A role named `{role_name}` already exists in this server", ephemeral=True)
        return
    if len(bot.data.roles) >= 20:
        await interaction.followup.send("❌ The role menu is full! (Discord limits messages to 20 reactions). Please remove old roles first.", ephemeral=True)
        return
    role_name = role_name.strip()
    if len(role_name) > 100:
        await interaction.followup.send("❌ Role names cannot be longer than 100 characters.", ephemeral=True)
        return
    
    day_int, parsed_time = parse_schedule(day, time)

    if day_int == -1:
        await interaction.followup.send(f"❌ I didn't understand the day `{day}`. Please use abbreviations like 'Mon', 'Tue', etc.")
        return
    if parsed_time == "err":
        await interaction.followup.send(f"❌ I didn't understand the time `{time}`. Try formats like `14:30` or `2:30 PM`.")
        return
    
    day_str = "n/a"
    time_str = "n/a"
    if day_int is not None:
        global day_names
        day_str = day_names[day_int]
    if parsed_time:
        time_str = parsed_time.strftime("%I:%M %p")

    if(not react_emoji):
        react_emoji = get_available_emoji(bot)
    elif(not emoji.is_emoji(react_emoji)):
        await interaction.followup.send(f"Is not valid emoji")

    perms = discord.Permissions(send_messages=True, read_messages=True)
    role = await interaction.guild.create_role(
        name=role_name, 
        colour=discord.Colour.blue(), 
        permissions=perms,
        mentionable=True,
        hoist=False
    )
    bot.data.roles[role_name] = RoleClass(
        role_id = role.id,
        ping_day = day_int,
        ping_time = parsed_time.isoformat() if parsed_time else None,
    )

    bot.data.reaction_map[react_emoji] = role.id
    await update_role_message()
    bot.save_data()

    message = (
        f"**{interaction.user.name} created New Role**\n"
        f"**Role:** `{role_name}`\n"
    )
    if(day_str and time_str):
        message += f"**Weekly Ping:** Every {day_str} at {time_str}\n\n"
    else:
        message += f"**No Weekly Ping**\n\n"
    await interaction.followup.send(message)

@bot.tree.command(name="rm", description="Removes a watchalong role (using @role)")
@app_commands.describe(
    role="Role @"
)
@app_commands.default_permissions(manage_roles=True)
async def rm(interaction: discord.Interaction, role: discord.Role):
    await interaction.response.defer()
    if role.name not in bot.data.roles:
        await interaction.followup.send(f"Role must be a watchalong role, list: {bot.data.roles}")
        return
    del bot.data.roles[role.name]
    await role.delete(reason=f"Deleted by {interaction.user.name}")
    key_to_del = next((k for k, v in bot.data.reaction_map.items() if v == role.id), None)
    if key_to_del:
        del bot.data.reaction_map[key_to_del]

        role_channel = bot.get_channel(int(ROLE_CHANNEL_ID))
        if role_channel:
            try:
                try:
                    role_message = await role_channel.fetch_message(bot.react_message_id)
                except Exception as e:
                    await init_react_message()
                    role_message = await role_channel.fetch_message(bot.react_message_id)
                await role_message.clear_reaction(key_to_del)
            except Exception as e:
                print(f"[WARNING] Could not clear reactions for {key_to_del}: {e}")

        await update_role_message()
        bot.save_data()
        await interaction.followup.send(f"The role {role.name} has been deleted.")

@bot.tree.command(name="list", description="Displays All Watchalong Roles")
@app_commands.default_permissions(manage_roles=True)
async def listroles(interaction: discord.Interaction):
    await interaction.response.defer()
    await interaction.followup.send(f"role list:\n{bot.data.roles}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingAnyRole):
        await interaction.response.send_message("❌ You do not have permission to use this command. Avaliable cmds include /rq /listq /list or ask an admin for approval", ephemeral=True)
    else:
        print(f"App Command Error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message("An error occurred while processing the command.", ephemeral=True)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    print(f"Ignoring traditional command error: {error}")

@bot.event
async def on_raw_reaction_add(payload):
    if payload.member.bot: return
    if payload.message_id != bot.react_message_id:
        return
    if str(payload.emoji) in bot.data.reaction_map:
        guild = bot.get_guild(payload.guild_id)
        role_id = bot.data.reaction_map[str(payload.emoji)]
        role = guild.get_role(role_id)

        if role:
            await payload.member.add_roles(role)
        else:
            print(f"[ERROR] Could not find role from role id: {role_id} for reaction: {payload.emoji}")

@bot.event
async def on_raw_reaction_remove(payload):
    user = bot.get_user(payload.user_id) 

    if user and user.bot:
        return
    if payload.message_id != bot.react_message_id:
        return

    if str(payload.emoji) in bot.data.reaction_map:
        guild = bot.get_guild(payload.guild_id)
        role_id = bot.data.reaction_map[str(payload.emoji)]
        role = guild.get_role(role_id)

        member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
        if role and member:
            await member.remove_roles(role)
        else:
            print(f"[ERROR] Could not find role and/or member from role id: {role_id} member_id {payload.user_id} for reaction: {payload.emoji}")

async def update_role_message():
    channel = bot.get_channel(ROLE_CHANNEL_ID)
    try:
        msg = await channel.fetch_message(bot.react_message_id)
    except Exception as e:
        await init_react_message()
        msg = await channel.fetch_message(bot.react_message_id)

    message = (
        f"**Role Menu: Anime Watchalongs**\n"
        f"React to give yourself a role.\n"
    )
    global day_names
    for emoji, role_id in bot.data.reaction_map.items():
        role = channel.guild.get_role(role_id)
        if not role:
            continue
        role_info = bot.data.roles.get(role.name)
        message += (f"\n{emoji} : `{role.name}`")
        if role_info and role_info.ping_day is not None and role_info.ping_time:
            time_obj = dt.time.fromisoformat(role_info.ping_time)
            formatted_time = time_obj.strftime("%I:%M %p")
            message += f" on `{day_names[role_info.ping_day]}` at `{formatted_time}`"
        message += "\n"

    await msg.edit(content=message)

    bot_reactions = [str(r.emoji) for r in msg.reactions if r.me]

    for emoji_str in bot.data.reaction_map.keys():
        if emoji_str not in bot_reactions:
            try:
                await msg.add_reaction(emoji_str)
            except Exception as e:
                print(f"[WARNING] Could not add reaction {emoji_str}: {e}")

async def init_react_message():
    channel = bot.get_channel(ROLE_CHANNEL_ID)
    message = await channel.send(f"**Role Menu: Anime Watchalongs**\n"
                        f"React to give yourself a role.\n")
    bot.react_message_id = message.id

bot.run(BOT_TOKEN)