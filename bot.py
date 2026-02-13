import os
import re
import json
import asyncio
import aiofiles
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Set, Optional
from collections import defaultdict, deque
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    filters, ContextTypes, CallbackQueryHandler
)
from telegram.constants import ParseMode
from asyncio import Semaphore, Queue
from threading import Lock
import logging
import time

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot token from @BotFather
BOT_TOKEN = "7828525928:AAEyxfC-kQU1s9tejGKsu4am0iSS8ifF8iI"

# YOUR ACTUAL DOMAIN
YOUR_DOMAIN = "https://terabox-proxy.tera-by-titan.workers.dev"
PLAYER_URL = f"{YOUR_DOMAIN}/?surl={{surl}}"

# Admin user IDs - REPLACE WITH YOUR ACTUAL TELEGRAM USER ID
ADMIN_IDS = [7163028849]  # <--- CHANGE THIS TO YOUR TELEGRAM ID

# Data file paths
USER_DATA_FILE = "user_data.json"
FORCE_CHANNELS_FILE = "force_channels.json"

# Supported Terabox domains
SUPPORTED_DOMAINS = [
    'terabox.com',
    '1024terabox.com',
    'teraboxapp.com',
    'tibox.com',
    'terabox.fun'
]

# IST timezone (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))

# ============= OPTIMIZATION SETTINGS =============

# Concurrency settings
MAX_CONCURRENT_TASKS = 100
BROADCAST_BATCH_SIZE = 20
BROADCAST_DELAY = 0.1
USER_CACHE_TTL = 300
MEMBERSHIP_CACHE_TTL = 300

# Rate limiting
RATE_LIMIT_PER_USER = 10
RATE_LIMIT_WINDOW = 60

# ============= SIMPLE CACHE IMPLEMENTATION =============

class TTLCache:
    """Simple TTL cache implementation without external dependencies"""
    
    def __init__(self, maxsize=10000, ttl=300):
        self.maxsize = maxsize
        self.ttl = ttl
        self.cache = {}
        self.timestamps = {}
        self.lock = Lock()
    
    def get(self, key):
        with self.lock:
            if key in self.cache:
                if time.time() - self.timestamps[key] < self.ttl:
                    return self.cache[key]
                else:
                    del self.cache[key]
                    del self.timestamps[key]
            return None
    
    def set(self, key, value):
        with self.lock:
            if len(self.cache) >= self.maxsize:
                oldest_key = min(self.timestamps.keys(), key=lambda k: self.timestamps[k])
                del self.cache[oldest_key]
                del self.timestamps[oldest_key]
            
            self.cache[key] = value
            self.timestamps[key] = time.time()
    
    def clear(self):
        with self.lock:
            self.cache.clear()
            self.timestamps.clear()
    
    def __len__(self):
        return len(self.cache)

class CacheManager:
    """High-performance caching system"""
    
    def __init__(self):
        self.user_cache = TTLCache(maxsize=10000, ttl=USER_CACHE_TTL)
        self.force_channel_cache = TTLCache(maxsize=100, ttl=USER_CACHE_TTL)
        self.membership_cache = TTLCache(maxsize=20000, ttl=MEMBERSHIP_CACHE_TTL)
        self.stats_cache = TTLCache(maxsize=10, ttl=300)
        
        self.rate_limits = defaultdict(list)
        self.rate_limit_lock = Lock()
        
        self.broadcast_queue = Queue()
        self.broadcast_results = {}
        
        self.semaphore = Semaphore(MAX_CONCURRENT_TASKS)
    
    async def get_user(self, user_id: int) -> Optional[Dict]:
        return self.user_cache.get(user_id)
    
    async def set_user(self, user_id: int, data: Dict):
        self.user_cache.set(user_id, data)
    
    async def get_membership(self, user_id: int) -> Optional[tuple]:
        return self.membership_cache.get(user_id)
    
    async def set_membership(self, user_id: int, status: tuple):
        self.membership_cache.set(user_id, status)
    
    async def check_rate_limit(self, user_id: int) -> bool:
        now = time.time()
        
        with self.rate_limit_lock:
            if user_id not in self.rate_limits:
                self.rate_limits[user_id] = []
            
            self.rate_limits[user_id] = [t for t in self.rate_limits[user_id] 
                                        if t > now - RATE_LIMIT_WINDOW]
            
            if len(self.rate_limits[user_id]) >= RATE_LIMIT_PER_USER:
                return False
            
            self.rate_limits[user_id].append(now)
            return True

cache_manager = CacheManager()

# ============= ASYNC FILE OPERATIONS =============

class AsyncDataManager:
    """Async file operations with batching"""
    
    def __init__(self):
        self.user_data = {"users": {}, "total_users": 0, "last_24h_users": 0, "last_update": None}
        self.force_channels = []
        self.data_lock = asyncio.Lock()
        self.save_queue = Queue()
        self._save_worker_task = None
        self._stats_update_task = None
    
    async def start(self):
        """Start background save worker"""
        self._save_worker_task = asyncio.create_task(self._save_worker())
        await self.load_initial_data()
    
    async def stop(self):
        """Stop background tasks"""
        if self._save_worker_task:
            self._save_worker_task.cancel()
        if self._stats_update_task:
            self._stats_update_task.cancel()
    
    async def load_initial_data(self):
        """Load all data at startup"""
        async with self.data_lock:
            # Load users
            try:
                async with aiofiles.open(USER_DATA_FILE, 'r') as f:
                    content = await f.read()
                    self.user_data = json.loads(content)
                    logger.info(f"Loaded {len(self.user_data.get('users', {}))} users from file")
            except FileNotFoundError:
                self.user_data = {"users": {}, "total_users": 0, "last_24h_users": 0, "last_update": None}
                logger.info("No user data file found, starting fresh")
            except Exception as e:
                logger.error(f"Error loading user data: {e}")
                self.user_data = {"users": {}, "total_users": 0, "last_24h_users": 0, "last_update": None}
            
            # Load force channels
            try:
                async with aiofiles.open(FORCE_CHANNELS_FILE, 'r') as f:
                    content = await f.read()
                    self.force_channels = json.loads(content)
                    logger.info(f"Loaded {len(self.force_channels)} force channels from file")
            except FileNotFoundError:
                self.force_channels = []
                logger.info("No force channels file found, starting fresh")
            except Exception as e:
                logger.error(f"Error loading force channels: {e}")
                self.force_channels = []
    
    async def _save_worker(self):
        """Background worker for saving data"""
        try:
            while True:
                try:
                    save_task = await self.save_queue.get()
                    
                    if save_task['type'] == 'users':
                        async with aiofiles.open(USER_DATA_FILE, 'w') as f:
                            await f.write(json.dumps(self.user_data, indent=4))
                        logger.debug("User data saved successfully")
                        
                    elif save_task['type'] == 'channels':
                        async with aiofiles.open(FORCE_CHANNELS_FILE, 'w') as f:
                            await f.write(json.dumps(self.force_channels, indent=4))
                        logger.debug("Force channels saved successfully")
                    
                    self.save_queue.task_done()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Save worker error: {e}")
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            logger.info("Save worker stopped")
    
    async def update_user(self, user_id: int) -> Dict:
        """Update user data asynchronously"""
        async with self.data_lock:
            current_time = datetime.now(IST).isoformat()
            user_id_str = str(user_id)
            
            if user_id_str not in self.user_data["users"]:
                self.user_data["users"][user_id_str] = {
                    "first_seen": current_time,
                    "last_seen": current_time,
                    "total_queries": 1
                }
                self.user_data["total_users"] = len(self.user_data["users"])
                logger.info(f"New user registered: {user_id}, total users: {self.user_data['total_users']}")
            else:
                self.user_data["users"][user_id_str]["last_seen"] = current_time
                self.user_data["users"][user_id_str]["total_queries"] = \
                    self.user_data["users"][user_id_str].get("total_queries", 0) + 1
            
            await self.save_queue.put({'type': 'users'})
            
            return self.user_data["users"][user_id_str]
    
    async def get_force_channels(self) -> List[Dict]:
        """Get force channels with caching"""
        async with self.data_lock:
            return self.force_channels.copy()
    
    async def add_force_channel(self, channel_data: Dict):
        """Add force channel asynchronously"""
        async with self.data_lock:
            self.force_channels.append(channel_data)
            await self.save_queue.put({'type': 'channels'})
            logger.info(f"Force channel added: {channel_data['title']}")
    
    async def remove_force_channel(self, channel_id: int) -> bool:
        """Remove force channel asynchronously"""
        async with self.data_lock:
            initial_len = len(self.force_channels)
            self.force_channels = [c for c in self.force_channels if c['id'] != channel_id]
            if len(self.force_channels) < initial_len:
                await self.save_queue.put({'type': 'channels'})
                logger.info(f"Force channel removed: {channel_id}")
                return True
            return False
    
    async def clear_force_channels(self):
        """Clear all force channels"""
        async with self.data_lock:
            count = len(self.force_channels)
            self.force_channels = []
            await self.save_queue.put({'type': 'channels'})
            logger.info(f"All force channels cleared: {count} channels removed")
    
    async def get_user_stats(self) -> Dict:
        """Get user statistics"""
        async with self.data_lock:
            return {
                "total_users": len(self.user_data.get("users", {})),
                "last_24h_users": self.user_data.get("last_24h_users", 0),
                "last_update": self.user_data.get("last_update")
            }
    
    async def get_all_user_ids(self) -> List[str]:
        """Get all user IDs"""
        async with self.data_lock:
            return list(self.user_data.get("users", {}).keys())

async_data_manager = AsyncDataManager()

# ============= HELPER FUNCTIONS =============

def is_admin(user_id: int) -> bool:
    """Check if user is admin"""
    return user_id in ADMIN_IDS

def extract_surl(text):
    """Extract surl from various Terabox URL formats"""
    patterns = [
        r'terabox\.com/s/([a-zA-Z0-9_\-]+)',
        r'1024terabox\.com/s/([a-zA-Z0-9_\-]+)',
        r'teraboxapp\.com/s/([a-zA-Z0-9_\-]+)',
        r'tibox\.com/s/([a-zA-Z0-9_\-]+)',
        r'terabox\.fun/s/([a-zA-Z0-9_\-]+)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    
    if re.match(r'^[a-zA-Z0-9_\-]+$', text.strip()):
        return text.strip()
    
    return None

def validate_terabox_link(text: str) -> tuple:
    """Validate Terabox link and return (is_valid, is_supported, message)"""
    terabox_pattern = r'https?://(?:[^/\s]+)/s/[a-zA-Z0-9_\-]+'
    
    if not re.search(terabox_pattern, text):
        return False, False, "❌ *This is not a Terabox link!*\n\nPlease send a valid Terabox share link."
    
    for domain in SUPPORTED_DOMAINS:
        if domain in text:
            return True, True, "✅ Supported Terabox link"
    
    domain_match = re.search(r'https?://([^/\s]+)', text)
    domain = domain_match.group(1) if domain_match else "Unknown domain"
    
    return True, False, f"❌ *Domain not supported:* `{domain}`\n\n*Supported domains:*\n• " + "\n• ".join(SUPPORTED_DOMAINS)

async def check_user_joined_channels(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> tuple:
    """Check if user has joined all force channels (with caching)"""
    cached_result = await cache_manager.get_membership(user_id)
    if cached_result:
        return cached_result
    
    force_channels = await async_data_manager.get_force_channels()
    
    if not force_channels:
        return True, []
    
    not_joined = []
    
    for channel in force_channels:
        try:
            member = await context.bot.get_chat_member(chat_id=channel['id'], user_id=user_id)
            if member.status in ['left', 'kicked']:
                not_joined.append(channel)
        except Exception as e:
            logger.error(f"Error checking channel {channel['id']} for user {user_id}: {e}")
            not_joined.append(channel)
    
    result = (len(not_joined) == 0, not_joined)
    await cache_manager.set_membership(user_id, result)
    
    return result

def get_force_channels_keyboard(not_joined_channels: List[Dict]) -> InlineKeyboardMarkup:
    """Create keyboard for force subscription"""
    keyboard = []
    
    for channel in not_joined_channels:
        keyboard.append([
            InlineKeyboardButton(
                f"📢 Join {channel['title']}",
                url=channel['invite_link']
            )
        ])
    
    keyboard.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_subscription")])
    
    return InlineKeyboardMarkup(keyboard)

async def update_user_stats(user_id: int):
    """Update user statistics asynchronously"""
    await async_data_manager.update_user(user_id)

async def calculate_24h_users():
    """Calculate users in last 24 hours"""
    current_time = datetime.now(IST)
    last_24h_cutoff = current_time - timedelta(hours=24)
    
    count = 0
    async with async_data_manager.data_lock:
        for user_data in async_data_manager.user_data["users"].values():
            last_seen = datetime.fromisoformat(user_data["last_seen"])
            if last_seen > last_24h_cutoff:
                count += 1
        
        async_data_manager.user_data["last_24h_users"] = count
        async_data_manager.user_data["last_update"] = current_time.isoformat()
        await async_data_manager.save_queue.put({'type': 'users'})
    
    logger.info(f"24h active users calculated: {count}")
    return count

async def periodic_stats_update():
    """Periodically update 24h user stats"""
    await asyncio.sleep(10)
    try:
        while True:
            now = datetime.now(IST)
            next_update = now.replace(hour=5, minute=30, second=0, microsecond=0)
            if now >= next_update:
                next_update += timedelta(days=1)
            
            wait_seconds = (next_update - now).total_seconds()
            logger.info(f"Next stats update scheduled at {next_update.strftime('%Y-%m-%d %H:%M:%S')} IST")
            await asyncio.sleep(wait_seconds)
            
            await calculate_24h_users()
    except asyncio.CancelledError:
        logger.info("Periodic stats update stopped")

# ============= HANDLERS =============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message with concurrency control"""
    async with cache_manager.semaphore:
        user_id = update.effective_user.id
        
        if not await cache_manager.check_rate_limit(user_id):
            await update.message.reply_text(
                "⚠️ *Rate Limit Exceeded*\n\nPlease wait a moment before sending more requests.",
                parse_mode='Markdown'
            )
            return
        
        await update_user_stats(user_id)
        
        has_joined, not_joined = await check_user_joined_channels(user_id, context)
        
        if not has_joined:
            keyboard = get_force_channels_keyboard(not_joined)
            await update.message.reply_text(
                "🔒 *Access Restricted*\n\nTo use this bot, you must join our channels first:",
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
            return
        
        welcome_text = (
            "🎬 *Terabox Video Bot - Hidden API Mode*\n\n"
            "Send me any Terabox link and I'll give you a private video player!\n\n"
            "🔒 *Features:*\n"
            "✅ API endpoints completely hidden\n"
            "✅ Video plays on my domain\n"
            "✅ No one sees the original API URL\n\n"
            "📤 *Send your Terabox link now:*"
        )
        await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user messages with concurrency control"""
    async with cache_manager.semaphore:
        user_id = update.effective_user.id
        
        if not await cache_manager.check_rate_limit(user_id):
            await update.message.reply_text(
                "⚠️ *Rate Limit Exceeded*\n\nPlease wait a moment before sending more requests.",
                parse_mode='Markdown'
            )
            return
        
        await update_user_stats(user_id)
        
        has_joined, not_joined = await check_user_joined_channels(user_id, context)
        
        if not has_joined:
            keyboard = get_force_channels_keyboard(not_joined)
            await update.message.reply_text(
                "🔒 *Access Restricted*\n\nYou need to join our channels to use this bot:",
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
            return
        
        message_text = update.message.text
        
        is_valid, is_supported, validation_msg = validate_terabox_link(message_text)
        
        if not is_valid or not is_supported:
            await update.message.reply_text(validation_msg, parse_mode='Markdown')
            return
        
        surl = extract_surl(message_text)
        
        if not surl:
            await update.message.reply_text(
                "❌ *Invalid Link Format*\n\nCould not extract video ID from your link.",
                parse_mode='Markdown'
            )
            return
        
        processing_msg = await update.message.reply_text("🎮 Creating hidden video player...")
        
        your_player_url = f"{YOUR_DOMAIN}/html-player/?surl={surl}"
        
        watch_text = (
            f"✅ *Hidden Video Player Created!*\n\n"
            f"🔗 *Video ID:* `{surl}`\n"
            f"🔒 *API Status:* Completely hidden\n\n"
            f"📱 *Click below to watch video:*\n"
        )
        
        keyboard = [
            [InlineKeyboardButton("🎬 Watch Video (Secure)", url=your_player_url)],
            [InlineKeyboardButton("💻 Devloped By Titan👑", url="https://t.me/Titanop24")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await processing_msg.edit_text(
            watch_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks"""
    async with cache_manager.semaphore:
        query = update.callback_query
        user_id = update.effective_user.id
        await query.answer()
        
        if query.data == "download_help":
            help_text = (
                "📥 *How to Download:*\n\n"
                "1️⃣ Click 'Watch Video' button above\n"
                "2️⃣ Wait for video to load\n"
                "3️⃣ **On Desktop:** Right-click video → Save video as...\n"
                "4️⃣ **On Mobile:** Tap video → ⋮ menu → Download\n\n"
                "🔒 *Note:* All videos stream through my hidden player.\n"
                "Original API URLs are never exposed!"
            )
            await query.message.reply_text(help_text, parse_mode='Markdown')
        
        elif query.data == "check_subscription":
            await cache_manager.set_membership(user_id, None)
            
            has_joined, not_joined = await check_user_joined_channels(user_id, context)
            
            if has_joined:
                await query.message.edit_text(
                    "✅ *Verification Successful!*\n\n"
                    "You now have access to the bot.\n"
                    "Send a Terabox link to start!",
                    parse_mode='Markdown'
                )
            else:
                keyboard = get_force_channels_keyboard(not_joined)
                await query.message.edit_text(
                    "❌ *Verification Failed*\n\nYou haven't joined all required channels yet:",
                    reply_markup=keyboard,
                    parse_mode='Markdown'
                )

# ============= ADMIN COMMANDS =============

async def admin_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all admin commands and their usage"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ *Unauthorized*\n\nThis command is only for admins.", parse_mode='Markdown')
        return
    
    force_channels = await async_data_manager.get_force_channels()
    user_stats = await async_data_manager.get_user_stats()
    
    commands_text = f"""
🔐 *ADMIN COMMANDS - USAGE GUIDE*
═══════════════════════════════

📢 *FORCE SUBSCRIPTION COMMANDS*
────────────────────────
🔹 `/force_add <channel_link>`
   Add channel to force subscription
   *Example:* `/force_add https://t.me/my_channel`

🔹 `/force_remove <channel_link>`
   Remove channel from force list
   *Example:* `/force_remove @my_channel`

🔹 `/force_clear`
   Remove ALL force channels at once

🔹 `/force_status`
   Check all force channels with joined users count

📨 *BROADCAST COMMANDS*
────────────────────────
🔹 `/broadcast`
   Send message to ALL users (reply to any message)

📊 *STATISTICS COMMANDS*
────────────────────────
🔹 `/users`
   View user statistics

ℹ️ *HELP COMMANDS*
────────────────────────
🔹 `/adm_cmd`
   Show this admin commands guide

⚡ *SYSTEM STATUS*
────────────────────────
• Total Users: `{user_stats['total_users']}`
• Force Channels: `{len(force_channels)}`
• Cache Size: `{len(cache_manager.membership_cache.cache)}`
• Rate Limit: `{RATE_LIMIT_PER_USER}/min`

═══════════════════════════════
✅ *Bot is running in high-performance mode*
    """
    
    await update.message.reply_text(
        commands_text,
        parse_mode='Markdown',
        disable_web_page_preview=True
    )

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Optimized broadcast with batching and queueing"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ *Unauthorized*", parse_mode='Markdown')
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "❌ *Usage:* Reply to any message (text, photo, video, etc.) with `/broadcast`",
            parse_mode='Markdown'
        )
        return
    
    broadcast_msg = update.message.reply_to_message
    user_stats = await async_data_manager.get_user_stats()
    total_users = user_stats['total_users']
    
    if total_users == 0:
        await update.message.reply_text("❌ No users to broadcast to.", parse_mode='Markdown')
        return
    
    status_msg = await update.message.reply_text(
        f"📢 *Broadcasting to {total_users} users...*\n"
        f"⏳ Using batched delivery ({BROADCAST_BATCH_SIZE} users/batch)",
        parse_mode='Markdown'
    )
    
    success = 0
    failed = 0
    batch = []
    user_ids = await async_data_manager.get_all_user_ids()
    
    for idx, user_id_str in enumerate(user_ids, 1):
        batch.append(int(user_id_str))
        
        if len(batch) >= BROADCAST_BATCH_SIZE or idx == len(user_ids):
            tasks = []
            for uid in batch:
                tasks.append(broadcast_msg.copy(chat_id=uid))
            
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for result in results:
                if isinstance(result, Exception):
                    failed += 1
                else:
                    success += 1
            
            batch = []
            
            progress = (idx / len(user_ids)) * 100
            await status_msg.edit_text(
                f"📢 *Broadcasting...*\n"
                f"✅ Sent: {success}\n"
                f"❌ Failed: {failed}\n"
                f"📊 Progress: {idx}/{len(user_ids)} ({progress:.1f}%)",
                parse_mode='Markdown'
            )
            
            await asyncio.sleep(BROADCAST_DELAY)
    
    await status_msg.edit_text(
        f"✅ *Broadcast Complete!*\n\n"
        f"📊 *Statistics:*\n"
        f"• Total users: `{total_users}`\n"
        f"• ✅ Sent: `{success}`\n"
        f"• ❌ Failed: `{failed}`\n"
        f"• 📈 Success rate: `{(success/total_users*100):.1f}%`",
        parse_mode='Markdown'
    )

async def admin_force_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add force subscription channel"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ *Unauthorized*", parse_mode='Markdown')
        return
    
    if len(context.args) < 1:
        await update.message.reply_text(
            "❌ *Usage:* `/force_add <channel_link>`\n\n"
            "Examples:\n"
            "`/force_add https://t.me/my_channel`\n"
            "`/force_add @my_channel`",
            parse_mode='Markdown'
        )
        return
    
    channel_input = context.args[0]
    
    try:
        if channel_input.startswith('https://t.me/'):
            username = channel_input.replace('https://t.me/', '').split('/')[0]
            chat = await context.bot.get_chat(f"@{username}")
        elif channel_input.startswith('@'):
            chat = await context.bot.get_chat(channel_input)
        else:
            chat = await context.bot.get_chat(f"@{channel_input}")
        
        force_channels = await async_data_manager.get_force_channels()
        
        for channel in force_channels:
            if channel['id'] == chat.id:
                await update.message.reply_text(
                    f"❌ Channel `{chat.title}` is already in force list.",
                    parse_mode='Markdown'
                )
                return
        
        try:
            invite_link = await chat.export_invite_link()
        except:
            if chat.username:
                invite_link = f"https://t.me/{chat.username}"
            else:
                await update.message.reply_text(
                    "❌ Bot must be admin in the channel to get invite link.\n"
                    "Make the bot admin and try again."
                )
                return
        
        channel_data = {
            'id': chat.id,
            'title': chat.title,
            'username': chat.username,
            'invite_link': invite_link,
            'added_by': user_id,
            'added_date': datetime.now(IST).isoformat()
        }
        
        await async_data_manager.add_force_channel(channel_data)
        cache_manager.membership_cache.clear()
        
        await update.message.reply_text(
            f"✅ *Channel Added to Force List*\n\n"
            f"📢 *Title:* `{chat.title}`\n"
            f"🔗 *Link:* [Join Channel]({invite_link})\n\n"
            f"Users must now join this channel to use the bot.",
            parse_mode='Markdown'
        )
        
    except Exception as e:
        await update.message.reply_text(
            f"❌ *Error adding channel*\n\n"
            f"Make sure:\n"
            f"1. Channel exists\n"
            f"2. Bot is admin in the channel\n"
            f"3. Channel link is correct\n\n"
            f"Error: {str(e)}",
            parse_mode='Markdown'
        )

async def admin_force_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove force subscription channel"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ *Unauthorized*", parse_mode='Markdown')
        return
    
    if len(context.args) < 1:
        await update.message.reply_text(
            "❌ *Usage:* `/force_remove <channel_link_or_username>`",
            parse_mode='Markdown'
        )
        return
    
    channel_input = context.args[0]
    force_channels = await async_data_manager.get_force_channels()
    
    if not force_channels:
        await update.message.reply_text("❌ No force channels configured.", parse_mode='Markdown')
        return
    
    removed = False
    removed_channel = None
    
    for channel in force_channels:
        username = channel.get('username', '')
        title = channel.get('title', '')
        
        if (channel_input in str(channel['id']) or 
            channel_input.strip('@') in username or 
            channel_input.lower() in title.lower()):
            await async_data_manager.remove_force_channel(channel['id'])
            removed = True
            removed_channel = channel
            break
    
    if removed:
        cache_manager.membership_cache.clear()
        await update.message.reply_text(
            f"✅ *Channel removed from force list*\n\n"
            f"📢 *Channel:* `{removed_channel['title']}`",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            "❌ Channel not found in force list.\n"
            "Use `/force_status` to see all force channels.",
            parse_mode='Markdown'
        )

async def admin_force_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear all force channels"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ *Unauthorized*", parse_mode='Markdown')
        return
    
    force_channels = await async_data_manager.get_force_channels()
    count = len(force_channels)
    
    if count == 0:
        await update.message.reply_text("❌ No force channels to clear.", parse_mode='Markdown')
        return
    
    await async_data_manager.clear_force_channels()
    cache_manager.membership_cache.clear()
    
    await update.message.reply_text(
        f"✅ *All force channels cleared*\n\n"
        f"Removed {count} channel{'s' if count > 1 else ''} from force list.",
        parse_mode='Markdown'
    )

async def admin_force_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check force channel status with joined users count"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ *Unauthorized*", parse_mode='Markdown')
        return
    
    force_channels = await async_data_manager.get_force_channels()
    
    if not force_channels:
        await update.message.reply_text(
            "📊 *Force Subscription Status*\n\nNo force channels configured.",
            parse_mode='Markdown'
        )
        return
    
    status_msg = await update.message.reply_text(
        "📊 *Fetching force channel status...*\nThis may take a few seconds.",
        parse_mode='Markdown'
    )
    
    status_text = "📊 *Force Subscription Status*\n\n"
    
    for idx, channel in enumerate(force_channels, 1):
        status_text += f"*{idx}. {channel['title']}*\n"
        status_text += f"   🔗 [Link]({channel['invite_link']})\n"
        
        joined_count = 0
        cache_key = f"channel_stats_{channel['id']}"
        cached_count = cache_manager.stats_cache.get(cache_key)
        
        if cached_count is not None:
            joined_count = cached_count
        else:
            user_ids = await async_data_manager.get_all_user_ids()
            for user_id_str in user_ids:
                try:
                    member = await context.bot.get_chat_member(
                        chat_id=channel['id'], 
                        user_id=int(user_id_str)
                    )
                    if member.status not in ['left', 'kicked']:
                        joined_count += 1
                except:
                    pass
            
            cache_manager.stats_cache.set(cache_key, joined_count)
        
        status_text += f"   👥 Joined Users: `{joined_count}`\n"
        status_text += f"   📅 Added: {channel['added_date'][:10]}\n\n"
    
    await status_msg.edit_text(
        status_text,
        parse_mode='Markdown',
        disable_web_page_preview=True
    )

async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get user statistics"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ *Unauthorized*", parse_mode='Markdown')
        return
    
    user_stats = await async_data_manager.get_user_stats()
    total_users = user_stats['total_users']
    last_24h = user_stats['last_24h_users']
    last_update = user_stats['last_update']
    
    current_time = datetime.now(IST)
    last_24h_cutoff = current_time - timedelta(hours=24)
    active_24h = 0
    new_users_24h = 0
    
    async with async_data_manager.data_lock:
        for user in async_data_manager.user_data.get("users", {}).values():
            last_seen = datetime.fromisoformat(user["last_seen"])
            if last_seen > last_24h_cutoff:
                active_24h += 1
            
            first_seen = datetime.fromisoformat(user["first_seen"])
            if first_seen > last_24h_cutoff:
                new_users_24h += 1
    
    stats_text = (
        f"📊 *User Statistics*\n\n"
        f"👥 *Total Users:* `{total_users}`\n"
        f"🟢 *Active (24h):* `{active_24h}`\n"
        f"🆕 *New (24h):* `{new_users_24h}`\n"
        f"📈 *24h Stats:* `{last_24h}`\n\n"
        f"🕐 *Last Updated:* `{last_update or 'Never'}`\n"
        f"⏰ *Timezone:* IST (UTC+5:30)"
    )
    
    await update.message.reply_text(stats_text, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help for regular users"""
    user_id = update.effective_user.id
    
    has_joined, not_joined = await check_user_joined_channels(user_id, context)
    
    if not has_joined:
        keyboard = get_force_channels_keyboard(not_joined)
        await update.message.reply_text(
            "🔒 *Access Restricted*\n\nYou need to join our channels to use this bot:",
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
        return
    
    help_text = """
🎬 *TERABOX VIDEO BOT - HELP*
═══════════════════════════

📤 *HOW TO USE*
────────────────
1️⃣ Send any Terabox link
2️⃣ Bot creates hidden video player
3️⃣ Click to watch securely

✅ *Supported Links:*
• 1024terabox.com/s/...
• terabox.com/s/...
• teraboxapp.com/s/...
• tibox.com/s/...
• terabox.fun/s/...

⚡ *COMMANDS*
────────────────
/start - Start the bot
/help - Show this help

═══════════════════════════
✨ *Enjoy secure video streaming!*
    """
    
    await update.message.reply_text(
        help_text,
        parse_mode='Markdown',
        disable_web_page_preview=True
    )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors gracefully"""
    logger.error(f"Update {update} caused error {context.error}")
    
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ *An error occurred*\n\nPlease try again later.",
                parse_mode='Markdown'
            )
    except:
        pass

# ============= BOT INITIALIZATION =============

async def post_init(application: Application):
    """Initialize bot on startup - FIXED: accepts application parameter"""
    await async_data_manager.start()
    
    # Start periodic stats update in background
    asyncio.create_task(periodic_stats_update())
    
    logger.info("=" * 60)
    logger.info("🤖 TERABOX BOT - HIGH PERFORMANCE MODE")
    logger.info("=" * 60)
    logger.info(f"✅ Bot Username: {(await application.bot.get_me()).username}")
    logger.info(f"✅ Admin IDs: {ADMIN_IDS}")
    logger.info(f"✅ Max Concurrent Tasks: {MAX_CONCURRENT_TASKS}")
    logger.info(f"✅ Broadcast Batch Size: {BROADCAST_BATCH_SIZE}")
    logger.info(f"✅ Cache Size Limit: 10,000 users")
    logger.info(f"✅ Rate Limit: {RATE_LIMIT_PER_USER} req/min per user")
    logger.info("=" * 60)

async def post_shutdown(application: Application):
    """Cleanup on shutdown"""
    await async_data_manager.stop()
    logger.info("Bot shutdown complete")

def main():
    """Start the bot"""
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Register startup/shutdown handlers - FIXED: Now accepts application parameter
    application.post_init = post_init
    application.post_shutdown = post_shutdown
    
    # User commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    
    # Admin commands
    application.add_handler(CommandHandler("force_add", admin_force_add))
    application.add_handler(CommandHandler("force_remove", admin_force_remove))
    application.add_handler(CommandHandler("force_clear", admin_force_clear))
    application.add_handler(CommandHandler("force_status", admin_force_status))
    application.add_handler(CommandHandler("broadcast", admin_broadcast))
    application.add_handler(CommandHandler("users", admin_users))
    application.add_handler(CommandHandler("adm_cmd", admin_commands))
    application.add_handler(CommandHandler("admin", admin_commands))
    
    # Message handlers
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Error handler
    application.add_error_handler(error_handler)
    
    print("\n" + "=" * 60)
    print("🤖 TERABOX BOT - STARTING UP")
    print("=" * 60)
    print(f"✅ Bot Token: Configured")
    print(f"✅ Admin IDs: {ADMIN_IDS}")
    print(f"✅ User Data File: {USER_DATA_FILE}")
    print(f"✅ Force Channels File: {FORCE_CHANNELS_FILE}")
    print(f"✅ Supported Domains: {len(SUPPORTED_DOMAINS)}")
    print("=" * 60 + "\n")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
