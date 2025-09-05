import praw
import csv
from datetime import datetime, timezone
import time
import logging
import os
import requests
from telegram import Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters
import threading
import schedule
import json
from dotenv import load_dotenv
import asyncio
from flask import Flask, jsonify
import atexit
import sys
import traceback
from collections import Counter
import heapq

# Force stdout/stderr to be unbuffered for better logging in Render
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# Load environment variables from .env file
load_dotenv()

# Configure logging with more detailed output
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),  # Explicitly log to stdout
        logging.StreamHandler(sys.stderr)   # Also log errors to stderr
    ]
)
logger = logging.getLogger(__name__)

# Silence the noisy HTTP loggers
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('requests').setLevel(logging.WARNING)

# Flask app and global variables
app = Flask(__name__)
tracker = None
telegram_bot = None
shutdown_event = threading.Event()

class RedditUserTracker:
    def __init__(self, reddit_credentials, telegram_token, telegram_chat_id, telegram_application):
        try:
            logger.info("Initializing RedditUserTracker...")
            self.telegram_application = telegram_application # Store the application instance
            self.telegram_loop = telegram_application.loop # Store the event loop
            
            # Initialize Reddit API
            self.reddit = praw.Reddit(
                client_id=reddit_credentials['client_id'],
                client_secret=reddit_credentials['client_secret'],
                user_agent=reddit_credentials['user_agent']
            )
            logger.info("Reddit API initialized successfully")

            # Use persistent disk path if available, otherwise current directory
            if os.path.exists('/data'):
                data_dir = '/data'
                logger.info("Using /data directory for persistent storage")
            else:
                data_dir = '.'
                logger.info("Using current directory for storage")
            
            # Initialize Telegram Bot
            self.telegram_bot = Bot(token=telegram_token)
            self.telegram_chat_id = telegram_chat_id
            logger.info("Telegram bot initialized")
            
            # Data storage
            self.tracked_users = set()
            self.user_add_timestamps = {}  # Track when each user was added
            self.data_file = os.path.join(data_dir, 'reddit_tracker_data.csv')
            self.users_file = os.path.join(data_dir, 'tracked_users.json') 
            self.last_check_file = os.path.join(data_dir, 'last_check.json')
            self.data = [] # This will hold our data instead of a pandas DataFrame
            
            logger.info(f"Data files will be stored in: {data_dir}")
            logger.info(f"CSV file: {self.data_file}")
            logger.info(f"Users file: {self.users_file}")
            
            # Load existing data
            self.load_tracked_users()
            self.load_existing_data()
            
            logger.info("RedditUserTracker initialization complete")
            
        except Exception as e:
            logger.error(f"CRITICAL ERROR in RedditUserTracker initialization: {e}")
            logger.error(traceback.format_exc())
            raise
        
    def load_tracked_users(self):
        """Load tracked users from file"""
        try:
            if os.path.exists(self.users_file):
                with open(self.users_file, 'r') as f:
                    data = json.load(f)
                    self.tracked_users = set(data.get('users', []))
                    self.user_add_timestamps = data.get('add_timestamps', {})
            logger.info(f"Loaded {len(self.tracked_users)} tracked users: {list(self.tracked_users)}")
        except Exception as e:
            logger.error(f"Error loading tracked users: {e}")
            self.tracked_users = set()
            self.user_add_timestamps = {}
    
    def save_tracked_users(self):
        """Save tracked users to file"""
        try:
            with open(self.users_file, 'w') as f:
                json.dump({
                    'users': list(self.tracked_users),
                    'add_timestamps': self.user_add_timestamps
                }, f)
            logger.info(f"Saved tracked users: {list(self.tracked_users)}")
        except Exception as e:
            logger.error(f"Error saving tracked users: {e}")
    
    def load_existing_data(self):
        """Load existing tracking data"""
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, 'r', newline='', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    self.data = [row for row in reader]
                logger.info(f"Loaded existing data with {len(self.data)} entries")
                # Show some stats about existing data
                if len(self.data) > 0:
                    user_stats = Counter(row['username'] for row in self.data)
                    logger.info(f"Data breakdown by user: {dict(user_stats)}")
                    latest_entry = max(float(row['created_utc']) for row in self.data)
                    logger.info(f"Latest entry timestamp: {latest_entry}")
            else:
                self.data = []
                logger.info("No existing data file, created empty list for data")
        except Exception as e:
            logger.error(f"Error loading existing data: {e}")
            self.data = []

    def save_data_with_retry(self, max_retries=3):
        """Save data to CSV with retry logic for permission errors"""
        if not self.data:
            return True
            
        for attempt in range(max_retries):
            try:
                with open(self.data_file, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.DictWriter(f, fieldnames=self.data[0].keys())
                    writer.writeheader()
                    writer.writerows(self.data)
                logger.info(f"Successfully saved data to {self.data_file}")
                return True
            except PermissionError as e:
                logger.warning(f"Permission denied saving CSV (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2)  # Wait before retry
                else:
                    logger.error(f"Failed to save CSV after {max_retries} attempts due to permission error")
                    return False
            except Exception as e:
                logger.error(f"Error saving data (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(1)
                else:
                    return False
        return False  
    
    def add_user(self, username):
        """Add a user to tracking list with privacy mode detection"""
        try:
            logger.info(f"Starting add_user for: {username}")
            username = username.lower().strip()
            if username.startswith('u/'):
                username = username[2:]
            
            if username in self.tracked_users:
                return f"User {username} is already being tracked"
            
            # Test if user exists first
            try:
                test_user = self.reddit.redditor(username)
                _ = test_user.id
                logger.info(f"Successfully validated user exists: {username}")
            except Exception as e:
                logger.error(f"Error validating user {username}: {e}")
                return f"Error: Could not find user '{username}' or user may not exist"
            
            # Check privacy mode status
            has_privacy, profile_accessible, reason = self.check_user_privacy_mode(username)
            logger.info(f"Privacy check for {username}: privacy_mode={has_privacy}, accessible={profile_accessible}, reason='{reason}'")
            
            self.tracked_users.add(username)
            self.user_add_timestamps[username] = time.time()
            self.save_tracked_users()
            
            # Initialize with recent content silently
            try:
                logger.info(f"Initializing tracking for {username}...")
                initial_content = self.get_user_content(username, limit=10, is_initialization=True)
                
                privacy_note = ""
                if has_privacy:
                    privacy_note = f"\n⚠️ Privacy Mode Detected: {reason}\n   Using hybrid search method for better coverage"
                
                if initial_content:
                    self.data.extend(initial_content)
                    self.save_data_with_retry()
                    logger.info(f"SILENTLY initialized {username} with {len(initial_content)} existing items")
                    
                    content_types = Counter(item['content_type'] for item in initial_content)
                    
                    return f"Added user: {username}{privacy_note}\nInitialized with {len(initial_content)} recent items ({dict(content_types)})\nWill now track NEW content only"
                else:
                    return f"Added user: {username}{privacy_note}\nCould not fetch initial content, will track from next check"
                    
            except Exception as e:
                logger.error(f"Error initializing user {username}: {e}")
                privacy_suffix = privacy_note if 'privacy_note' in locals() and has_privacy else ""
                return f"Added user: {username}{privacy_suffix}\nError during initialization: {e}"
        except Exception as e:
            logger.error(f"CRITICAL ERROR in add_user for {username}: {e}")
            logger.error(traceback.format_exc())
            return f"Critical error adding user {username}: {e}"
    
    def remove_user(self, username):
        """Remove a user from tracking list"""
        username = username.lower().strip()
        if username.startswith('u/'):
            username = username[2:]
        
        if username in self.tracked_users:
            self.tracked_users.remove(username)
            # Also remove the timestamp
            if username in self.user_add_timestamps:
                del self.user_add_timestamps[username]
            self.save_tracked_users()
            return f"Removed user: {username}"
        else:
            return f"User {username} was not being tracked"
    
    def list_users(self):
        """List all tracked users"""
        if self.tracked_users:
            return "Tracked users:\n" + "\n".join(f"- {user}" for user in sorted(self.tracked_users))
        else:
            return "No users currently being tracked"
    
    def reset_all_data(self):
        """Reset all stored data"""
        try:
            # Clear the data
            self.data = []
            
            # Remove existing files
            if os.path.exists(self.data_file):
                os.remove(self.data_file)
            if os.path.exists(self.last_check_file):
                os.remove(self.last_check_file)
                
            logger.info("All tracking data has been reset")
            return "Data reset successfully"
        except Exception as e:
            logger.error(f"Error resetting data: {e}")
            return f"Error resetting data: {e}"
    
    def get_user_content(self, username, limit=25, is_initialization=False):
        """Get recent posts and comments for a user using hybrid approach"""
        try:
            user = self.reddit.redditor(username)
            content_list = []
            
            logger.info(f"Fetching content for user: {username} (limit: {limit}, init: {is_initialization})")
            
            # PHASE 1: Try profile method first
            profile_posts = []
            profile_comments = []
            
            try:
                logger.info(f"Phase 1: Trying profile method for {username}")
                
                # Get recent submissions (posts) from profile
                posts_count = 0
                for submission in user.submissions.new(limit=limit):
                    posts_count += 1
                    profile_posts.append({
                        'username': username,
                        'content_type': 'post',
                        'title': submission.title,
                        'content': submission.selftext if submission.selftext else '[Link Post]',
                        'subreddit': str(submission.subreddit),
                        'url': f"https://reddit.com{submission.permalink}",
                        'score': submission.score,
                        'created_utc': submission.created_utc,
                        'id': submission.id,
                        'timestamp_logged': datetime.now().isoformat()
                    })
                logger.info(f"Profile method found {posts_count} posts for {username}")
                
                # Get recent comments from profile
                comments_count = 0
                for comment in user.comments.new(limit=limit):
                    comments_count += 1
                    parent_title = "Unknown"
                    
                    try:
                        if hasattr(comment, 'submission') and comment.submission:
                            parent_title = comment.submission.title[:100]
                    except Exception as title_e:
                        logger.debug(f"Could not get submission title for comment {comment.id}: {title_e}")
                        parent_title = "Unknown"
                    
                    profile_comments.append({
                        'username': username,
                        'content_type': 'comment',
                        'title': f"Comment on: {parent_title}",
                        'content': comment.body,
                        'subreddit': str(comment.subreddit),
                        'url': f"https://reddit.com{comment.permalink}",
                        'score': comment.score,
                        'created_utc': comment.created_utc,
                        'id': comment.id,
                        'timestamp_logged': datetime.now().isoformat()
                    })
                    
                logger.info(f"Profile method found {comments_count} comments for {username}")
                
            except Exception as e:
                logger.warning(f"Profile method failed for {username}: {e}")
            
            # PHASE 2: Determine if we need search fallback
            total_profile_content = len(profile_posts) + len(profile_comments)
            use_search_fallback = False
            
            # Use search fallback if:
            # 1. Profile method returned very few or no results
            # 2. Or if this is initialization and we got suspiciously few results
            if total_profile_content < 3:  # Less than 3 total items suggests privacy mode
                logger.info(f"Profile method returned only {total_profile_content} items for {username} - trying search fallback")
                use_search_fallback = True
            elif is_initialization and total_profile_content < 8:  # During init, expect more content
                logger.info(f"Initialization for {username} only found {total_profile_content} items - trying search fallback")
                use_search_fallback = True
            
            search_posts = []
            search_comments = []
            
            if use_search_fallback:
                try:
                    logger.info(f"Phase 2: Using search fallback for {username}")
                    
                    # Search for posts by this user
                    search_query = f"author:{username}"
                    logger.info(f"Searching with query: '{search_query}'")
                    
                    # Search across all subreddits, sorted by new
                    search_results = list(self.reddit.subreddit('all').search(
                        search_query, 
                        sort='new', 
                        limit=limit * 2  # Get more to account for mixed posts/comments
                    ))
                    
                    logger.info(f"Search returned {len(search_results)} total results for {username}")
                    
                    for item in search_results:
                        # Check if this is a submission (post)
                        if hasattr(item, 'selftext'):  # This is a submission
                            # Skip if we already have this from profile method
                            if not any(p['id'] == item.id for p in profile_posts):
                                search_posts.append({
                                    'username': username,
                                    'content_type': 'post',
                                    'title': item.title,
                                    'content': item.selftext if item.selftext else '[Link Post]',
                                    'subreddit': str(item.subreddit),
                                    'url': f"https://reddit.com{item.permalink}",
                                    'score': item.score,
                                    'created_utc': item.created_utc,
                                    'id': item.id,
                                    'timestamp_logged': datetime.now().isoformat()
                                })
                    
                    logger.info(f"Search method found {len(search_posts)} additional posts for {username}")
                    
                    # Note: Reddit search doesn't return comments directly, only submissions
                    # Comments are harder to get via search, so we'll rely on profile method for those
                    # or could use a separate search approach for comments if needed
                    
                except Exception as e:
                    logger.error(f"Search fallback failed for {username}: {e}")
            
            # PHASE 3: Combine results and deduplicate
            content_list = []
            seen_ids = set()
            
            # Add profile content first (it's usually more reliable when available)
            for item in profile_posts + profile_comments:
                if item['id'] not in seen_ids:
                    content_list.append(item)
                    seen_ids.add(item['id'])
            
            # Add search content if it's new
            for item in search_posts + search_comments:
                if item['id'] not in seen_ids:
                    content_list.append(item)
                    seen_ids.add(item['id'])
            
            # Sort by creation time (newest first)
            content_list.sort(key=lambda x: x['created_utc'], reverse=True)
            
            # Log summary
            total_posts = len([c for c in content_list if c['content_type'] == 'post'])
            total_comments = len([c for c in content_list if c['content_type'] == 'comment'])
            
            logger.info(f"Final results for {username}: {len(content_list)} items total ({total_posts} posts, {total_comments} comments)")
            
            if use_search_fallback:
                logger.info(f"  - Profile method: {len(profile_posts)} posts, {len(profile_comments)} comments")
                logger.info(f"  - Search method: {len(search_posts)} additional posts")
            
            # Log the timestamps of the most recent content
            if content_list:
                timestamps = [item['created_utc'] for item in content_list[:5]]
                readable_times = [datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S') for ts in timestamps]
                logger.info(f"Most recent content timestamps: {readable_times}")
            
            return content_list[:limit]  # Return up to the requested limit
            
        except Exception as e:
            logger.error(f"CRITICAL ERROR accessing user {username}: {e}")
            logger.error(traceback.format_exc())
            return []

    def check_user_privacy_mode(self, username):
        """
        Check if a user likely has privacy mode enabled by testing profile access
        Returns: (has_privacy_mode, profile_accessible, reason)
        """
        try:
            user = self.reddit.redditor(username)
            
            # Try to get basic user info
            try:
                _ = user.id  # This will fail if user doesn't exist
            except:
                return True, False, "User doesn't exist or is suspended"
            
            # Try to get recent submissions
            submission_count = 0
            try:
                for _ in user.submissions.new(limit=5):
                    submission_count += 1
                    if submission_count >= 3:  # If we get 3+ posts, profile is accessible
                        break
            except Exception as e:
                logger.debug(f"Error accessing submissions for {username}: {e}")
            
            # Try to get recent comments
            comment_count = 0
            try:
                for _ in user.comments.new(limit=5):
                    comment_count += 1
                    if comment_count >= 3:  # If we get 3+ comments, profile is accessible
                        break
            except Exception as e:
                logger.debug(f"Error accessing comments for {username}: {e}")
            
            total_accessible = submission_count + comment_count
            
            if total_accessible == 0:
                return True, False, "No posts or comments accessible via profile"
            elif total_accessible < 3:
                return True, True, f"Very limited content accessible ({total_accessible} items)"
            else:
                return False, True, f"Profile fully accessible ({total_accessible}+ items found)"
                
        except Exception as e:
            logger.error(f"Error checking privacy mode for {username}: {e}")
            return True, False, f"Error checking user: {e}"

    def check_for_new_content(self):
        """Check all tracked users for new content"""
        try:
            logger.info(f"Starting content check for {len(self.tracked_users)} users...")
            logger.info(f"Currently tracking: {list(self.tracked_users)}")
            
            if not self.tracked_users:
                logger.info("No users to track")
                return
                
            new_entries = []
            silent_entries = []  # For old content that shouldn't trigger notifications
            
            for username in self.tracked_users:
                logger.info(f"Checking user: {username}")
                content = self.get_user_content(username, limit=25)
                
                # Debug: show what we got
                logger.info(f"Retrieved {len(content)} items for {username}")
                
                for item in content:
                    # Check if we already have this content
                    existing = any(row['id'] == item['id'] and row['username'] == username for row in self.data)
                    
                    # Only notify about content created AFTER the user was added
                    user_add_time = self.user_add_timestamps.get(username, 0)
                    content_created_time = item['created_utc']
                    
                    if not existing:
                        # This content is not in our database yet
                        if content_created_time > user_add_time:
                            # This is genuinely NEW content (created after user was added)
                            logger.info(f"NEW {item['content_type']} found for {username}: {item['id']} - {item['title'][:50]}...")
                            logger.info(f"   Created: {datetime.fromtimestamp(item['created_utc']).strftime('%Y-%m-%d %H:%M:%S')} UTC (after user was added)")
                            logger.info(f"   Score: {item['score']}, Subreddit: r/{item['subreddit']}")
                            new_entries.append(item)
                        else:
                            # This is old content that we missed during initialization - add to DB but don't notify
                            logger.info(f"OLD {item['content_type']} found for {username}: {item['id']} (created before user was added) - adding to DB silently")
                            silent_entries.append(item)
                    else:
                        logger.debug(f"Already have {item['content_type']}: {item['id']}")
            
            # Add silent entries to database without notifications
            if silent_entries:
                logger.info(f"Adding {len(silent_entries)} old entries to database silently")
                self.data.extend(silent_entries)
            
            if new_entries:
                logger.info(f"Found {len(new_entries)} NEW entries total!")
                
                # Show breakdown
                breakdown = Counter(f"{entry['username']} ({entry['content_type']})" for entry in new_entries)
                logger.info(f"New content breakdown: {dict(breakdown)}")
                
                # Add to data
                self.data.extend(new_entries)
                
                # Save to CSV
                self.save_data_with_retry()
                logger.info(f"Saved data to {self.data_file}")
                
                # Send messages immediately
                self.queue_messages_for_telegram(new_entries)
            else:
                logger.info("No new content found this check")
                
            # Always save if we have silent entries
            if silent_entries and not new_entries:
                self.save_data_with_retry()
                logger.info(f"Saved {len(silent_entries)} silent entries to {self.data_file}")
                
        except Exception as e:
            logger.error(f"CRITICAL ERROR in check_for_new_content: {e}")
            logger.error(traceback.format_exc())
    
    def format_telegram_message(self, entry):
        """Format content for Telegram message"""
        try:
            content_preview = entry['content'][:250]
            if len(entry['content']) > 250:
                content_preview += "..."
            
            # Escape HTML characters
            content_preview = content_preview.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            title = entry['title'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            
            message = f"""New {entry['content_type']} by u/{entry['username']}   
                 
    Subreddit: r/{entry['subreddit']}
    Title: {title}
    Content: {content_preview}
    Score: {entry['score']}
    URL: {entry['url']}
    Time: {datetime.fromtimestamp(entry['created_utc']).strftime('%Y-%m-%d %H:%M:%S')} UTC
    """
            return message.strip()
        except Exception as e:
            logger.error(f"Error formatting message: {e}")
            return f"Error formatting message for {entry.get('username', 'unknown')}: {e}"
  
    def queue_messages_for_telegram(self, new_entries):
        """Send messages to Telegram by creating tasks on the bot's event loop."""
        try:
            if not self.telegram_loop or not self.telegram_loop.is_running():
                logger.error("Telegram event loop is not available or not running. Cannot send messages.")
                return

            logger.info(f"Queueing {len(new_entries)} messages for sending via thread-safe call.")
            
            for entry in new_entries:
                message = self.format_telegram_message(entry)
                
                # Schedule the send_telegram_message coroutine to run on the application's event loop from our thread
                future = asyncio.run_coroutine_threadsafe(self.send_telegram_message(message, entry), self.telegram_loop)
                # Optionally, you could add a callback to the future to check for exceptions, but for now, we rely on the coroutine's own logging.

            logger.info(f"All {len(new_entries)} messages have been scheduled for sending.")
            
        except Exception as e:
            logger.error(f"CRITICAL ERROR in queue_messages_for_telegram: {e}")
            logger.error(traceback.format_exc())
    
    async def send_telegram_message(self, message, entry):
        """Send a single message to Telegram with proper error handling"""
        from telegram.error import RetryAfter, TelegramError
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                await self.telegram_bot.send_message(
                    chat_id=self.telegram_chat_id,
                    text=message,
                    parse_mode='HTML',
                    disable_web_page_preview=True
                )
                logger.info(f"✓ Successfully sent notification for {entry['username']} ({entry['content_type']})")
                return True
                
            except RetryAfter as e:
                wait_time = e.retry_after + 1
                logger.warning(f"Rate limited on {entry['username']}. Waiting {wait_time}s (attempt {attempt + 1})")
                await asyncio.sleep(wait_time)
                
            except TelegramError as e:
                logger.error(f"Telegram error for {entry['username']} (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                
            except Exception as e:
                logger.error(f"Unexpected error sending for {entry['username']}: {e}")
                return False
                
        logger.error(f"✗ Failed to send message for {entry['username']} ({entry['content_type']}) after {max_retries} attempts")
        return False

class TelegramBot:
    def __init__(self, telegram_token):
        self.tracker = None # Initialize tracker as None
        self.application = Application.builder().token(telegram_token).build()
        self.pending_messages = [] 

        # Add handlers
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("add", self.add_user))
        self.application.add_handler(CommandHandler("remove", self.remove_user))
        self.application.add_handler(CommandHandler("list", self.list_users))
        self.application.add_handler(CommandHandler("check", self.manual_check))
        self.application.add_handler(CommandHandler("reset", self.reset_data))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("debug", self.debug_command))

    def set_tracker(self, tracker):
        """Set the tracker instance after initialization."""
        self.tracker = tracker

    def start_bot(self):
        """Start the Telegram bot polling."""
        logger.info("Starting Telegram bot polling...")
        self.application.run_polling()

# Flask routes for monitoring
@app.route('/')
def health_check():
    return jsonify({
        "status": "running",
        "tracked_users": len(tracker.tracked_users) if tracker else 0,
        "message": "Reddit Tracker is running",
        "data_entries": len(tracker.data) if tracker else 0
    })

@app.route('/health')
def health():
    return jsonify({"status": "healthy"})

@app.route('/status')
def status():
    if not tracker:
        return jsonify({"error": "Tracker not initialized"})
    
    return jsonify({
        "tracked_users": list(tracker.tracked_users),
        "total_entries": len(tracker.data),
        "data_file_exists": os.path.exists(tracker.data_file),
        "users_file_exists": os.path.exists(tracker.users_file),
        "last_check": "Running continuously"
    })

@app.route('/force-check')
def force_check():
    """Trigger a manual check via web endpoint"""
    if not tracker:
        return jsonify({"error": "Tracker not initialized"})
    
    try:
        tracker.check_for_new_content()
        return jsonify({"message": "Manual check completed"})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route('/backup-data')
def backup_data():
    """Export current data as JSON for backup"""
    if not tracker:
        return jsonify({"error": "Tracker not initialized"})
    
    try:
        data = {
            "tracked_users": list(tracker.tracked_users),
            "user_add_timestamps": tracker.user_add_timestamps,
            "data_entries": len(tracker.data),
            "backup_timestamp": datetime.now().isoformat()
        }
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)})        

def run_scheduler(tracker, telegram_bot, shutdown_event):
    """Run the scheduled content checks with graceful shutdown"""
    def scheduled_check():
        try:
            logger.info("Running scheduled content check...")
            tracker.check_for_new_content()
            logger.info("Scheduled check completed")
        except Exception as e:
            logger.error(f"Error in scheduled check: {e}")
    
    # Schedule the check
    schedule.every(1).minutes.do(scheduled_check)
    
    logger.info("Scheduler started - checking every minute")
    
    while not shutdown_event.is_set():
        try:
            schedule.run_pending()
            time.sleep(1)
        except Exception as e:
            logger.error(f"Error in scheduler: {e}")
            time.sleep(5)
    
    logger.info("Scheduler stopped gracefully")

# Graceful shutdown handler
def cleanup_on_shutdown():
    """Cleanup function called on shutdown"""
    logger.info("Shutting down gracefully...")
    shutdown_event.set()
    if tracker:
        try:
            # Save any pending data
            tracker.save_data_with_retry()
            tracker.save_tracked_users()
            logger.info("Data saved before shutdown")
        except Exception as e:
            logger.error(f"Error saving data on shutdown: {e}")

# Register cleanup function
atexit.register(cleanup_on_shutdown)
def main():
    global tracker, telegram_bot  # Make them global
    
    # Configuration from environment variables
    REDDIT_CLIENT_ID = os.getenv('REDDIT_CLIENT_ID')
    REDDIT_CLIENT_SECRET = os.getenv('REDDIT_CLIENT_SECRET')
    REDDIT_USER_AGENT = os.getenv('REDDIT_USER_AGENT', 'RedditTracker/1.0')
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
    
    # Check if all required environment variables are set
    required_vars = {
        'REDDIT_CLIENT_ID': REDDIT_CLIENT_ID,
        'REDDIT_CLIENT_SECRET': REDDIT_CLIENT_SECRET,
        'TELEGRAM_BOT_TOKEN': TELEGRAM_TOKEN,
        'TELEGRAM_CHAT_ID': TELEGRAM_CHAT_ID
    }
    
    missing_vars = [var for var, value in required_vars.items() if not value]
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        logger.error("Please create a .env file with the required variables")
        return
    
    REDDIT_CREDENTIALS = {
        'client_id': REDDIT_CLIENT_ID,
        'client_secret': REDDIT_CLIENT_SECRET,
        'user_agent': REDDIT_USER_AGENT
    }
    
    # Initialize Telegram bot
    logger.info("Initializing Telegram bot...")
    telegram_bot = TelegramBot(TELEGRAM_TOKEN)
    
    # Initialize tracker
    logger.info("Initializing Reddit tracker...")
    tracker = RedditUserTracker(REDDIT_CREDENTIALS, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, telegram_bot.application)

    # Set the tracker on the bot to break circular dependency
    telegram_bot.set_tracker(tracker)
    
    # Start scheduler in a separate thread
    logger.info("Starting scheduler thread...")
    scheduler_thread = threading.Thread(
        target=run_scheduler, 
        args=(tracker, telegram_bot, shutdown_event),
        daemon=True
    )
    scheduler_thread.start()
    
    # Start Telegram bot in a separate thread
    logger.info("Starting Telegram bot thread...")
    bot_thread = threading.Thread(
        target=telegram_bot.start_bot,
        daemon=True
    )
    bot_thread.start()
    
    logger.info("Starting Flask web server...")
    logger.info(f"Tracked users on startup: {list(tracker.tracked_users)}")
    logger.info(f"Data entries on startup: {len(tracker.data)}")
    
    # Get port from environment (Render provides this)
    port = int(os.environ.get('PORT', 5000))
    
    # Start Flask app
    try:
        app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
    except KeyboardInterrupt:
        logger.info("Received shutdown signal")
        cleanup_on_shutdown()

if __name__ == "__main__":
    main()
