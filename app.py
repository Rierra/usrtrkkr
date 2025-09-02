import praw
import pandas as pd
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
    def __init__(self, reddit_credentials, telegram_token, telegram_chat_id):
        try:
            logger.info("Initializing RedditUserTracker...")
            
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
                self.df = pd.read_csv(self.data_file)
                logger.info(f"Loaded existing data with {len(self.df)} entries")
                # Show some stats about existing data
                if len(self.df) > 0:
                    user_stats = self.df['username'].value_counts()
                    logger.info(f"Data breakdown by user: {dict(user_stats)}")
                    latest_entry = self.df['created_utc'].max()
                    logger.info(f"Latest entry timestamp: {latest_entry}")
            else:
                self.df = pd.DataFrame(columns=[
                    'username', 'content_type', 'title', 'content', 'subreddit',
                    'url', 'score', 'created_utc', 'id', 'timestamp_logged'
                ])
                logger.info("No existing data file, created empty DataFrame")
        except Exception as e:
            logger.error(f"Error loading existing data: {e}")
            self.df = pd.DataFrame(columns=[
                'username', 'content_type', 'title', 'content', 'subreddit',
                'url', 'score', 'created_utc', 'id', 'timestamp_logged'
            ])

    def save_data_with_retry(self, max_retries=3):
        """Save data to CSV with retry logic for permission errors"""
        for attempt in range(max_retries):
            try:
                self.df.to_csv(self.data_file, index=False)
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
                    new_df = pd.DataFrame(initial_content)
                    self.df = pd.concat([self.df, new_df], ignore_index=True)
                    self.save_data_with_retry()
                    logger.info(f"SILENTLY initialized {username} with {len(initial_content)} existing items")
                    
                    content_types = {}
                    for item in initial_content:
                        content_types[item['content_type']] = content_types.get(item['content_type'], 0) + 1
                    
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
            # Clear the dataframe
            self.df = pd.DataFrame(columns=[
                'username', 'content_type', 'title', 'content', 'subreddit',
                'url', 'score', 'created_utc', 'id', 'timestamp_logged'
            ])
            
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
                    existing = self.df[
                        (self.df['username'] == username) & 
                        (self.df['id'] == item['id'])
                    ]
                    
                    # Only notify about content created AFTER the user was added
                    user_add_time = self.user_add_timestamps.get(username, 0)
                    content_created_time = item['created_utc']
                    
                    if existing.empty:
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
                silent_df = pd.DataFrame(silent_entries)
                self.df = pd.concat([self.df, silent_df], ignore_index=True)
            
            if new_entries:
                logger.info(f"Found {len(new_entries)} NEW entries total!")
                
                # Show breakdown
                breakdown = {}
                for entry in new_entries:
                    key = f"{entry['username']} ({entry['content_type']})"
                    breakdown[key] = breakdown.get(key, 0) + 1
                logger.info(f"New content breakdown: {breakdown}")
                
                # Add to dataframe
                new_df = pd.DataFrame(new_entries)
                self.df = pd.concat([self.df, new_df], ignore_index=True)
                
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
        """Send messages immediately to Telegram"""
        try:
            for entry in new_entries:
                message = self.format_telegram_message(entry)
                
                # Send message immediately using sync method
                try:
                    # Use a more robust async approach
                    if asyncio.get_running_loop():
                        # If we're already in an async context, this won't work
                        logger.warning("Already in async context, cannot send immediately")
                        return
                except RuntimeError:
                    # No running loop, which is what we want
                    pass
                
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        success = loop.run_until_complete(self.send_telegram_message(message))
                        if success:
                            logger.info(f"✓ Successfully sent notification for {entry['username']} ({entry['content_type']})")
                        else:
                            logger.error(f"✗ Failed to send notification for {entry['username']} ({entry['content_type']})")
                    finally:
                        loop.close()
                        # Reset to no event loop
                        asyncio.set_event_loop(None)
                except Exception as e:
                    logger.error(f"Error sending Telegram message for {entry['username']}: {e}")
            
            logger.info(f"Attempted to send {len(new_entries)} messages to Telegram")
        except Exception as e:
            logger.error(f"CRITICAL ERROR in queue_messages_for_telegram: {e}")
            logger.error(traceback.format_exc())
    
    async def send_telegram_message(self, message):
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
                return True
                
            except RetryAfter as e:
                wait_time = e.retry_after + 1
                logger.warning(f"Rate limited. Waiting {wait_time} seconds (attempt {attempt + 1})")
                await asyncio.sleep(wait_time)
                
            except TelegramError as e:
                logger.error(f"Telegram error (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                return False
                
        logger.error(f"Failed to send message after {max_retries} attempts")
        return False

class TelegramBot:
    def __init__(self, tracker, telegram_token):
        self.tracker = tracker
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

    def queue_message(self, message):
        """Add message to pending queue"""
        self.pending_messages.append(message)
        logger.info(f"Queued message. Total pending: {len(self.pending_messages)}")    
    
    
    async def send_pending_messages(self):
        """Send all pending messages"""
        if not self.pending_messages:
            return
            
        logger.info(f"Sending {len(self.pending_messages)} pending messages...")
        
        sent = 0
        failed = 0
        
        messages_to_send = self.pending_messages.copy()
        self.pending_messages.clear()
        
        for i, message in enumerate(messages_to_send):
            try:
                success = await self.tracker.send_telegram_message(message)
                if success:
                    sent += 1
                    logger.info(f"Sent message {i+1}/{len(messages_to_send)}")
                else:
                    failed += 1
                    
                # Rate limiting - wait between messages
                if i < len(messages_to_send) - 1:
                    await asyncio.sleep(2)
                    
            except Exception as e:
                logger.error(f"Error sending message {i+1}: {e}")
                failed += 1
        
        logger.info(f"Message sending complete: {sent} sent, {failed} failed")
    
    async def start(self, update, context):
        """Start command handler"""
        message = """Reddit User Tracker Bot (Fixed Version)

I monitor Reddit users and notify you of their new posts and comments!

Quick Start:
/add username - Start tracking a user
/list - See who you're tracking  
/check - Manual check for new content
/debug - Show debug info
/help - Full command list

When you add a user, I'll initialize with their recent content and then only notify about NEW stuff!

Auto-checks happen every minute with improved message delivery."""
        await update.message.reply_text(message)
    
    async def add_user(self, update, context):
        """Add user command handler"""
        if not context.args:
            await update.message.reply_text("Please provide a username. Example: /add spez")
            return
        
        username = " ".join(context.args)
        await update.message.reply_text(f"Adding user {username}...")
        
        # Run in thread to avoid blocking
        import concurrent.futures
        try:
            with concurrent.futures.ThreadPoolExecutor() as executor:
                result = await asyncio.get_event_loop().run_in_executor(
                    executor, self.tracker.add_user, username
                )
            await update.message.reply_text(result)
        except Exception as e:
            await update.message.reply_text(f"Error adding user: {str(e)}")
    
    async def remove_user(self, update, context):
        """Remove user command handler"""
        if not context.args:
            await update.message.reply_text("Please provide a username. Example: /remove spez")
            return
        
        username = " ".join(context.args)
        result = self.tracker.remove_user(username)
        await update.message.reply_text(result)
    
    async def list_users(self, update, context):
        """List users command handler"""
        result = self.tracker.list_users()
        await update.message.reply_text(result)
    
    async def manual_check(self, update, context):
        """Manual check command handler"""
        await update.message.reply_text("Checking for new content...")
        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                await asyncio.get_event_loop().run_in_executor(
                    executor, self.tracker.check_for_new_content
                )
            
            # Send any queued messages
            await self.send_pending_messages()
            
            await update.message.reply_text("Manual check completed! Check sent any new notifications.")
        except Exception as e:
            await update.message.reply_text(f"Error during manual check: {str(e)}")
    
    async def debug_command(self, update, context):
        """Show debug information"""
        try:
            debug_info = f"""Debug Information

Tracked Users: {len(self.tracker.tracked_users)}
{chr(10).join(f"  - {user}" for user in sorted(self.tracker.tracked_users))}

Total Data Entries: {len(self.tracker.df)}
Pending Messages: {len(self.pending_messages)}

Data Breakdown by User:"""
            
            if len(self.tracker.df) > 0:
                user_stats = self.tracker.df['username'].value_counts()
                for username, count in user_stats.items():
                    debug_info += f"\n  - {username}: {count} items"
                
                debug_info += f"\n\nContent Type Breakdown:"
                type_stats = self.tracker.df['content_type'].value_counts()
                for content_type, count in type_stats.items():
                    debug_info += f"\n  - {content_type}: {count}"
                
                # Show most recent entries
                latest_entries = self.tracker.df.nlargest(3, 'created_utc')[['username', 'content_type', 'created_utc']]
                debug_info += f"\n\nMost Recent Entries:"
                for _, row in latest_entries.iterrows():
                    timestamp = datetime.fromtimestamp(row['created_utc']).strftime('%Y-%m-%d %H:%M:%S')
                    debug_info += f"\n  - {row['username']} ({row['content_type']}) - {timestamp}"
            else:
                debug_info += "\n  No data entries found"
                
            await update.message.reply_text(debug_info)
            
        except Exception as e:
            await update.message.reply_text(f"Error getting debug info: {str(e)}")
    
    async def reset_data(self, update, context):
        """Reset all stored data"""
        try:
            self.tracker.reset_all_data()
            await update.message.reply_text("All stored data has been cleared!")
        except Exception as e:
            await update.message.reply_text(f"Error resetting data: {str(e)}")
    
    async def help_command(self, update, context):
        """Help command handler"""
        message = """Reddit User Tracker Commands

/add <username> - Start tracking a user
  - Initializes with recent content (no spam)
  - Only notifies about NEW content afterward
  - Example: /add spez

/remove <username> - Stop tracking a user  
  - Example: /remove spez

/list - Show all tracked users

/check - Manually check for new content
  - Useful for immediate updates
  - Also sends any pending messages

/debug - Show debug information
  - See tracked users and data stats
  - View pending message count

/reset - Clear all stored data
  - Fresh start for all tracking

/help - Show this help

Auto-checking: Every minute
Data saved to: reddit_tracker_data.csv
Fixed: Telegram message delivery issues"""
        await update.message.reply_text(message)
    
    def start_bot(self):
        """Start the Telegram bot"""
        logger.info("Starting Telegram bot...")
        self.application.run_polling()

# Flask routes for monitoring
@app.route('/')
def health_check():
    return jsonify({
        "status": "running",
        "tracked_users": len(tracker.tracked_users) if tracker else 0,
        "message": "Reddit Tracker is running",
        "data_entries": len(tracker.df) if tracker else 0
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
        "total_entries": len(tracker.df),
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
            "data_entries": len(tracker.df),
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
    
    # Initialize tracker
    logger.info("Initializing Reddit tracker...")
    tracker = RedditUserTracker(REDDIT_CREDENTIALS, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
    
    # Initialize Telegram bot
    logger.info("Initializing Telegram bot...")
    telegram_bot = TelegramBot(tracker, TELEGRAM_TOKEN)
    
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
    logger.info(f"Data entries on startup: {len(tracker.df)}")
    
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
