import time
from typing import Any, Dict, List
import discord
from discord import app_commands
import os
import json
import asyncio
from datetime import datetime, timezone
from dotenv import load_dotenv
import logging
from .config import GuildConfiguration, Configuration
from .quote import PriceQuoteCache

logformat = '%(asctime)s.%(msecs)03d %(name)-6s:[%(levelname)-8s] %(message)s'
logging.basicConfig(
    format=logformat,
    datefmt='%Y-%m-%dT%H:%M:%S',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# Load environment variables
load_dotenv()

# Get tokens from .env file
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CMC_API_KEY = os.getenv("CMC_API_KEY")

# Initialize Discord client
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Data storage
DATA_FILE = "crypto_bot_data.json"

# Stylesheet storage
STYLES_FILE = "crypto_bot_styles.json"

# Default styles structure
default_styles = {
    "price_up_icon": "📈",
    "price_down_icon": "📉",
}

Config: Configuration = None
PriceQuoter: PriceQuoteCache = None
STYLES = {}


def to_all_strings(d: Dict[str,int]) -> Dict[str,str]:
    '''
    Convert the given dictionary of strings->ints into a dictionary of
    strings->decimal-integer-strings so as to avoid data loss in JSON.
    '''
    return { k: str(v) for k, v in d.items() }


def to_all_ints(d: Dict[str,str]) -> Dict[str,int]:
    '''
    Convert the given dictionary of strings->decimal-integer-strings into a
    dictionary of strings->ints under the assumption that the former was
    in place simply to avoid JSON data loss.
    '''
    return { k: int(v) for k, v in d.items() }


def config_from_dict(d: Dict) -> Configuration:
    '''
    Produce a bot configuration struct from a dictionary loaded, presumably,
    from JSON.
    '''
    guilds: Dict[int,GuildConfiguration] = {}
    for guild_id_s, guild_data in d.items():
        guild_id = int(guild_id_s)
        update_category = guild_data.get('update_category')
        if update_category is not None:
            update_category = int(update_category)
        voice_tickers = guild_data.get('voice_tickers', [])
        ratio_tickers = to_all_ints(guild_data.get('ratio_tickers', {}))
        message_tickers = to_all_ints(guild_data.get('message_tickers', {}))
        guilds[guild_id] = GuildConfiguration(
            id=guild_id,
            update_category=update_category,
            voice_tickers=voice_tickers,
            ratio_tickers=ratio_tickers,
            message_tickers=message_tickers,
        )
    return Configuration(guilds=guilds)


def dict_from_config(c: Configuration) -> Dict:
    '''
    Produce a JSON-compatible dictionary from a server configuration.
    '''
    d = {}
    for guild in c.guilds.values():
        guild_data = {
            'message_tickers': to_all_strings(guild.message_tickers),
            'ratio_tickers': to_all_strings(guild.ratio_tickers),
            'voice_tickers': guild.voice_tickers,
        }
        if guild.update_category is not None:
            guild_data['update_category'] = str(guild.update_category)
        d[str(guild.id)] = guild_data
    return d


def load_styles() -> dict:
    '''
    Load style data from JSON or give reasonable defaults.
    '''
    data = dict(default_styles)
    data.update(load_json(STYLES_FILE))
    return data


def load_config() -> Configuration:
    '''
    Load bot data from JSON or give reasonable defaults.
    '''
    data = load_json(DATA_FILE)
    return config_from_dict(data)


def load_json(path: str) -> dict:
    '''
    Load data from JSON file or fail quietly and return empty dict.
    '''
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

# Save data to JSON file
def save_config(c: Configuration):
    d = dict_from_config(c)
    with open(DATA_FILE, 'w') as f:
        json.dump(d, f, indent=4)

# Format current time in UTC
def get_utc_time():
    now = datetime.now(timezone.utc)
    return now.strftime("%I:%M %p UTC")

# Check if user has admin permissions
def is_admin(interaction):
    for role in interaction.user.roles:
        if role.name == 'botty':
            return True
    return False

voice_loop = None
tickers_loop = None

@client.event
async def on_ready():
    global voice_loop
    global tickers_loop
    await tree.sync()
    logger.info(f"{client.user} ready.")
    
    # Start update loops
    if voice_loop is None:
        voice_loop = client.loop.create_task(update_voice_channels_loop())
    if tickers_loop is None:
        tickers_loop = client.loop.create_task(update_message_tickers_loop())


@client.event
async def on_disconnect():
    logging.info(f"{client.user} disconnected.")


async def boundary_timer(cadence: int, name: str):
    now = time.time()
    next_boundary = (cadence - (now % cadence))
    logger.info("Sleeping %f seconds until next %s boundary", next_boundary, name)
    await asyncio.sleep(next_boundary)
    while client.is_closed():
        logging.info("Retrying %s update; client not currently connected.", name)
        await asyncio.sleep(180)


# Voice channel update loop (hourly)
async def update_voice_channels_loop():
    logger.info("Begining voice update loop, waiting for ready")
    await client.wait_until_ready()
    logger.info("Voice update loop starting.")
    while True:
        await boundary_timer(3600, "voice update")
        await update_all_voice_channels()


# Message update loop (30 minutes)
async def update_message_tickers_loop():
    logger.info("Begining message update loop, waiting for ready")
    await client.wait_until_ready()
    logger.info("Message update loop starting.")
    while True:
        await boundary_timer(1800, "message")
        await update_all_message_tickers()


# Update all voice channels with current prices
async def update_all_voice_channels():
    logging.info("Updating all voice channels")
    for guild_config in Config.guilds.values():
        if guild_config.update_category is None or not guild_config.voice_tickers:
            continue
            
        guild = client.get_guild(guild_config.id)
        if not guild:
            logging.warning("Configuration for bot lists a guild that may no longer exist: %d", guild_config.id)
            continue
            
        category_id = guild_config.update_category
        category = discord.utils.get(guild.categories, id=int(category_id))
        if not category:
            continue
            
        tickers = guild_config.voice_tickers
        if not tickers:
            continue
            
        quotes = await PriceQuoter.fetch(tickers, time.time())
        if not quotes:
            continue
        
        # Sort by market cap (highest first)
        ticker_data = sorted(quotes, key=lambda x: x.market_cap, reverse=True)
        
        # Delete all existing voice channels in the category
        for channel in category.voice_channels:
            await channel.delete()
        
        # Create new channels with updated prices
        _current_time = get_utc_time()
        for quote in ticker_data:
            if quote.percent_change_1h >= 0:
                emoji = STYLES['price_up_icon']
            else:
                emoji = STYLES['price_down_icon']
            
            # Format price based on its value
            price = quote.price_usd
            if price < 0.01:
                price_str = f"${price:.6f}"
            elif price < 1:
                price_str = f"${price:.4f}"
            elif price < 1000:
                price_str = f"${price:.2f}"
            else:
                price_str = f"${price:.0f}"
            
            logging.debug("Updating voice ticker for %s: %s", quote.symbol, price_str)
            channel_name = f"{quote.symbol} {emoji} {price_str}"
            await category.create_voice_channel(name=channel_name)


# Update all message tickers
async def update_all_message_tickers(do_regulars: bool=True, do_ratios: bool=True):
    logging.info("Updating all message channels")
    for guild_config in Config.guilds.values():
        guild = client.get_guild(guild_config.id)
        if not guild:
            continue
        
        # Regular ticker messages
        message_tickers = guild_config.message_tickers
        if message_tickers and do_regulars:
            symbols = list(message_tickers.keys())
            quotes = await PriceQuoter.fetch(symbols, time.time())
            quotes_by_symbol = {
                quote.symbol: quote
                for quote in quotes
            }
            
            for symbol, channel_id in message_tickers.items():
                quote = quotes_by_symbol.get(symbol)
                if quote is None:
                    logger.warning("Skipping update for symbol %s as there is no data for it.", symbol)
                    continue

                channel = client.get_channel(channel_id)
                if not channel:
                    logger.warning("Unable to fetch channel %d data, skipping a quote", channel_id)
                    continue

                cmc_url = f"<https://coinmarketcap.com/currencies/{quote.slug}/>"
                message = f"The price of {quote.name} ({symbol}) is {quote.price_usd:.2f} USD on [CMC]({cmc_url})"
                await channel.send(message)
        
        # Ratio ticker messages
        if do_ratios:
            ratio_tickers = guild_config.ratio_tickers
        else:
            ratio_tickers = {}
        for pair, channel_id in ratio_tickers.items():
            ticker1, ticker2 = pair.split(":")
            quotes = await PriceQuoter.fetch([ticker1, ticker2], time.time())
            quotes_by_symbol = {
                quote.symbol: quote
                for quote in quotes
            }
            a = quotes_by_symbol.get(ticker1)
            b = quotes_by_symbol.get(ticker2)
            if a is None or b is None:
                logger.warning("Skipping update for symbol pair %s:%s as there is no data for it.", ticker1, ticker2)
                continue

            channel = client.get_channel(channel_id)
            if not channel:
                logger.warning("Unable to fetch channel %d data, skipping a quote", channel_id)
                continue

            ratio = int(b.price_usd / a.price_usd)
            cmc_url = f"<https://coinmarketcap.com/currencies/{a.slug}/>"
            message = f"The swap rate of {ticker1}:{ticker2} is {ratio}:1 on [CMC]({cmc_url})"
            await channel.send(message)


@tree.command(name="set_voice_update_category", description="Set the category for price update voice channels")
async def set_voice_update_category(interaction, category_id: str):
    # Check if user has admin permissions
    if not is_admin(interaction):
        await interaction.response.send_message("You need administrator permissions to use this command.", ephemeral=True)
        return
    
    try:
        category_id = int(category_id)
        guild_id = interaction.guild_id
        
        # Verify the category exists
        category = discord.utils.get(interaction.guild.categories, id=category_id)
        if not category:
            await interaction.response.send_message("Category not found. Please provide a valid category ID.", ephemeral=True)
            return
        
        # Update data
        guild = Config.guilds.get(guild_id)
        if guild is None:
            guild = GuildConfiguration(id=guild_id)
            Config.guilds[guild_id] = guild
        
        guild.update_category = category_id
        guild.voice_tickers = []
        
        save_config(Config)
        await interaction.response.send_message(f"Update category set to {category.name}", ephemeral=True)
    
    except ValueError:
        await interaction.response.send_message("Please provide a valid category ID (numbers only).", ephemeral=True)

@tree.command(name="add_voice_ticker", description="Add a ticker to voice channel updates")
async def add_voice_ticker(interaction, ticker: str):
    # Defer the response immediately to prevent timeout
    await interaction.response.defer(ephemeral=True)
    
    # Check if user has admin permissions
    if not is_admin(interaction):
        await interaction.followup.send("You need administrator permissions to use this command.", ephemeral=True)
        return
    
    ticker = ticker.upper()
    guild_id = interaction.guild_id
    guild = Config.guilds.get(guild_id)
    if guild is None or guild.update_category is None:
        await interaction.followup.send("Please set an update category first using /set_update_category", ephemeral=True)
        return
    
    # Verify the ticker exists
    crypto_data = await PriceQuoter.fetch_no_cache([ticker])
    if len(crypto_data) == 0:
        await interaction.followup.send(f"Ticker {ticker} not found on CoinMarketCap.", ephemeral=True)
        return
    
    if ticker not in guild.voice_tickers:
        guild.voice_tickers.append(ticker)
        save_config(Config)
        
        # Force update the voice channels
        await update_all_voice_channels()
        await interaction.followup.send(f"Added {ticker} to voice channel updates.", ephemeral=True)
    else:
        await interaction.followup.send(f"{ticker} is already being tracked.", ephemeral=True)

@tree.command(name="remove_voice_ticker", description="Remove a ticker from voice channel updates")
async def remove_voice_ticker(interaction, ticker: str):
    # Check if user has admin permissions
    if not is_admin(interaction):
        await interaction.response.send_message("You need administrator permissions to use this command.", ephemeral=True)
        return
    
    ticker = ticker.upper()
    guild_id = interaction.guild_id
    
    guild = Config.guilds.get(guild_id)
    if guild is not None:
        if ticker in guild.voice_tickers:
            guild.voice_tickers.remove(ticker)
            save_config(Config)
            
            # Force update to remove the channel
            await update_all_voice_channels()
            await interaction.response.send_message(f"Removed {ticker} from voice channel updates.", ephemeral=True)
            return
    
    await interaction.response.send_message(f"{ticker} is not currently being tracked.", ephemeral=True)

@tree.command(name="force_update_tickers", description="Force update all voice channels")
async def force_update_tickers(interaction):
    # Check if user has admin permissions
    if not is_admin(interaction):
        await interaction.response.send_message("You need administrator permissions to use this command.", ephemeral=True)
        return
    
    await interaction.response.send_message("Updating all voice channels...", ephemeral=True)
    await update_all_voice_channels()

@tree.command(name="add_message_ticker", description="Add a ticker for regular price messages")
async def add_message_ticker(interaction, ticker: str, channel_id: str):
    # Check if user has admin permissions
    if not is_admin(interaction):
        await interaction.response.send_message("You need administrator permissions to use this command.", ephemeral=True)
        return
    
    ticker = ticker.upper()
    guild_id = interaction.guild_id
    
    try:
        channel_id = int(channel_id)
        channel = client.get_channel(channel_id)
        
        if not channel:
            await interaction.response.send_message("Channel not found. Please provide a valid channel ID.", ephemeral=True)
            return
        
        # Verify the ticker exists
        crypto_data = await PriceQuoter.fetch_no_cache([ticker])
        if not crypto_data:
            await interaction.response.send_message(f"Ticker {ticker} not found on CoinMarketCap.", ephemeral=True)
            return
        
        guild = Config.guilds.get(guild_id)
        if guild is None:
            guild = GuildConfiguration(id=guild_id)
            Config.guilds[guild_id] = guild
        
        guild.message_tickers[ticker] = channel_id
        save_config(Config)
        
        await interaction.response.send_message(f"Added {ticker} price messages to <#{channel_id}>", ephemeral=True)
    
    except ValueError:
        await interaction.response.send_message("Please provide a valid channel ID (numbers only).", ephemeral=True)


@tree.command(name="remove_message_ticker", description="Remove a ticker from regular price messages")
async def remove_message_ticker(interaction, ticker: str):
    # Check if user has admin permissions
    if not is_admin(interaction):
        await interaction.response.send_message("You need administrator permissions to use this command.", ephemeral=True)
        return
    
    ticker = ticker.upper()
    guild_id = interaction.guild_id
    guild = Config.guilds.get(guild_id)
    if guild is not None and (ticker in guild.message_tickers):
        del guild.message_tickers[ticker]
        save_config(Config)
        await interaction.response.send_message(f"Removed {ticker} from price messages.", ephemeral=True)
        return
    
    await interaction.response.send_message(f"{ticker} is not currently being tracked for messages.", ephemeral=True)

@tree.command(name="add_message_ratio_tickers", description="Add a ticker ratio for regular messages")
async def add_message_ratio_tickers(interaction, ticker1: str, ticker2: str, channel_id: str):
    # Check if user has admin permissions
    if not is_admin(interaction):
        await interaction.response.send_message("You need administrator permissions to use this command.", ephemeral=True)
        return
    
    ticker1 = ticker1.upper()
    ticker2 = ticker2.upper()
    guild_id = interaction.guild_id
    
    try:
        channel_id = int(channel_id)
        channel = client.get_channel(channel_id)
        
        if not channel:
            await interaction.response.send_message("Channel not found. Please provide a valid channel ID.", ephemeral=True)
            return
        
        # Verify the tickers exist
        crypto_data = await PriceQuoter.fetch_no_cache([ticker1, ticker2])
        by_symbol = { quote.symbol: quote for quote in crypto_data }
        if by_symbol.get(ticker1) is None or by_symbol.get(ticker2) is None:
            await interaction.response.send_message(f"One or both tickers not found on CoinMarketCap.", ephemeral=True)
            return
        
        guild = Config.guilds.get(guild_id)
        if guild is None:
            guild = GuildConfiguration(id=guild_id)
                
        pair_key = f"{ticker1}:{ticker2}"
        guild.ratio_tickers[pair_key] = channel_id
        save_config(Config)
        
        await interaction.response.send_message(f"Added {ticker1}:{ticker2} ratio messages to <#{channel_id}>", ephemeral=True)
    
    except ValueError:
        await interaction.response.send_message("Please provide a valid channel ID (numbers only).", ephemeral=True)

@tree.command(name="remove_message_ratio_tickers", description="Remove a ticker ratio from regular messages")
async def remove_message_ratio_tickers(interaction, ticker1: str, ticker2: str):
    # Defer the response immediately to prevent timeout
    await interaction.response.defer(ephemeral=True)
    
    # Check if user has admin permissions
    if not is_admin(interaction):
        await interaction.followup.send("You need administrator permissions to use this command.", ephemeral=True)
        return
    
    ticker1 = ticker1.upper()
    ticker2 = ticker2.upper()
    pair_key = f"{ticker1}:{ticker2}"
    guild_id = interaction.guild_id
    
    guild = Config.guilds.get(guild_id)
    if guild is not None:
        if pair_key in guild.ratio_tickers:
            del guild.ratio_tickers[pair_key]
            save_config(Config)
            await interaction.followup.send(f"Removed {ticker1}:{ticker2} ratio from price messages.", ephemeral=True)
            return
    
    await interaction.followup.send(f"Ratio {ticker1}:{ticker2} is not currently being tracked.", ephemeral=True)


@tree.command(name="force_update_message_tickers", description="Force update all message tickers")
async def force_update_message_tickers(interaction):
    # Check if user has admin permissions
    if not is_admin(interaction):
        await interaction.response.send_message("You need administrator permissions to use this command.", ephemeral=True)
        return
    
    await interaction.response.send_message("Updating all message tickers...", ephemeral=True)
    await update_all_message_tickers(do_regulars=True, do_ratios=False)


@tree.command(name="force_update_ratio_tickers", description="Force update all ratio-based tickers")
async def force_update_ratio_tickers(interaction):
    # Check if user has admin permissions
    if not is_admin(interaction):
        await interaction.response.send_message("You need administrator permissions to use this command.", ephemeral=True)
        return
    
    await interaction.response.send_message("Updating all ratio tickers...", ephemeral=True)
    await update_all_message_tickers(do_regulars=False, do_ratios=True)


@tree.command(name="speak", description="Say something in a channel.")
async def speak(interaction, channel_id: str, message: str):
    # Check if user has admin permissions
    if not is_admin(interaction):
        await interaction.response.send_message("You need administrator permissions to use this command.", ephemeral=True)
        return

    channel_id = int(channel_id)
    channel = client.get_channel(channel_id)

    if not channel:
        await interaction.response.send_message("Channel not found. Please provide a valid channel ID.", ephemeral=True)

    await channel.send(message)
    await interaction.response.send_message("Message sent.", ephemeral=True)


@tree.command(name="show_settings", description="Show all current bot settings")
async def show_settings(interaction):
    # Check if user has admin permissions
    if not is_admin(interaction):
        await interaction.response.send_message("You need administrator permissions to use this command.", ephemeral=True)
        return
    
    guild_id = interaction.guild_id
    
    # Create embed
    embed = discord.Embed(
        title="Crypto Bot Settings",
        description=f"Current settings for {interaction.guild.name}",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    
    # Add bot icon as thumbnail if available
    if client.user.avatar:
        embed.set_thumbnail(url=client.user.avatar.url)
    
    # Check if guild has any settings
    guild = Config.guilds.get(guild_id)
    if guild is None:
        embed.add_field(name="Status", value="No settings configured yet.", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
       
    # Add update category info
    if guild.update_category is not None:
        category = discord.utils.get(interaction.guild.categories, id=guild.update_category)
        category_name = category.name if category else "Unknown (category may have been deleted)"
        
        embed.add_field(
            name="Update Category",
            value=f"**Name:** {category_name}\n**ID:** {guild.update_category}",
            inline=False
        )
    
    # Add voice tickers
    voice_tickers = guild.voice_tickers
    if voice_tickers:
        tickers_list = ", ".join(voice_tickers)
        
        embed.add_field(
            name="Voice Channel Tickers",
            value=f"{tickers_list}",
            inline=False
        )
    else:
        embed.add_field(
            name="Voice Channel Tickers",
            value="None configured",
            inline=False
        )
    
    # Add message tickers
    message_tickers = guild.message_tickers
    if message_tickers:
        tickers_text = ""
        for ticker, channel_id in message_tickers.items():
            channel = interaction.guild.get_channel(channel_id)
            channel_name = channel.name if channel else "Unknown channel"
            tickers_text += f"**{ticker}** → #{channel_name} ({channel_id})\n"
        
        embed.add_field(
            name="Message Tickers",
            value=tickers_text,
            inline=False
        )
    else:
        embed.add_field(
            name="Message Tickers",
            value="None configured",
            inline=False
        )
    
    # Add ratio tickers
    ratio_tickers = guild.ratio_tickers
    if ratio_tickers:
        ratio_text = ""
        for pair, channel_id in ratio_tickers.items():
            channel = interaction.guild.get_channel(channel_id)
            channel_name = channel.name if channel else "Unknown channel"
            ratio_text += f"**{pair}** → #{channel_name} ({channel_id})\n"
        
        embed.add_field(
            name="Ratio Tickers",
            value=ratio_text,
            inline=False
        )
    else:
        embed.add_field(
            name="Ratio Tickers",
            value="None configured",
            inline=False
        )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)


Config = load_config()
STYLES = load_styles()
PriceQuoter = PriceQuoteCache(CMC_API_KEY)
logger.info(Config)

# Run the bot
client.run(DISCORD_TOKEN, log_handler=None)
