import asyncio
import aiohttp
import os
import logging
import json
import time
from typing import Optional, Dict, Any, List, Tuple
import re
import tempfile
import shutil
from datetime import datetime, timedelta
from threading import Thread

from telegram import Update, Document
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ChatAction

# Keep-alive server imports
from flask import Flask
import requests
from threading import Timer

# Configure detailed logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration - Use environment variables for production
ACADEMI_USERNAME = os.getenv("ACADEMI_USERNAME", "mazbhaulhaque@gmail.com")
ACADEMI_PASSWORD = os.getenv("ACADEMI_PASSWORD", "Tuistnig23ZL.pb")
BOT_TOKEN = os.getenv("BOT_TOKEN", "7504576229:AAHFIJSD_ZLQv95XVgSBe-SOOoO5JkacsOE")

# Render configuration
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_URL", "")  # Set this in Render environment variables

# Bot limits
MAX_FILES_PER_PERIOD = 3
RATE_LIMIT_HOURS = 6
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB limit

# Keep-alive Flask app
app = Flask(__name__)

@app.route('/')
def home():
    return "Telegram Bot is running!"

@app.route('/health')
def health():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

def keep_alive():
    """Send a request to keep the server alive every 14 minutes"""
    if RENDER_URL:
        try:
            response = requests.get(f"{RENDER_URL}/health", timeout=10)
            logger.info(f"Keep-alive ping successful: {response.status_code}")
        except Exception as e:
            logger.error(f"Keep-alive ping failed: {e}")
    
    # Schedule next ping
    Timer(840, keep_alive).start()  # 14 minutes = 840 seconds

def run_flask():
    """Run Flask app in a separate thread"""
    app.run(host='0.0.0.0', port=PORT, debug=False)

class TurnitinChecker:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.session = None
        self.upload_endpoint = "https://academi.cx/dashboard/file_upload.php"
        
    async def login(self) -> bool:
        """Login to academi.cx"""
        print("🔐 Logging in...")
        
        self.session = aiohttp.ClientSession()
        
        login_data = {
            'email': self.username,
            'password': self.password,
            'rememberme': 'on'
        }
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': 'https://academi.cx/login',
            'Content-Type': 'application/x-www-form-urlencoded',
        }
        
        try:
            async with self.session.post('https://academi.cx/login/login.php', 
                                       data=login_data, headers=headers) as response:
                print(f"Login Status: {response.status}")
                
                if response.status in [200, 302]:
                    response_text = await response.text()
                    if 'Dashboard' in response_text or 'dashboard' in response_text.lower():
                        print("✅ Login successful!")
                        return True
                    elif response.status == 302:
                        print("✅ Login successful (redirected)!")
                        return True
                
                print("❌ Login failed")
                return False
                
        except Exception as e:
            print(f"❌ Login error: {e}")
            return False
    
    async def upload_file(self, file_path: str) -> Optional[str]:
        """Upload file and return the file ID"""
        if not os.path.exists(file_path):
            print(f"❌ File not found: {file_path}")
            return None
            
        print(f"📤 Uploading file: {os.path.basename(file_path)}")
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': 'https://academi.cx/dashboard',
        }
        
        try:
            with open(file_path, 'rb') as f:
                data = aiohttp.FormData()
                data.add_field('file', f, filename=os.path.basename(file_path))
                
                async with self.session.post(self.upload_endpoint, 
                                           data=data, headers=headers) as response:
                    print(f"Upload Status: {response.status}")
                    
                    if response.status == 200:
                        try:
                            result = await response.json()
                            print(f"✅ Upload Response: {result}")
                        except:
                            response_text = await response.text()
                            print(f"Upload Response (text): {response_text[:500]}")
                        
                        # Wait for the file to be processed and get the actual file ID
                        await asyncio.sleep(3)  # Wait for file to be processed
                        file_id = await self.get_latest_file_id()
                        if file_id:
                            print(f"📋 Retrieved file ID: {file_id}")
                            return file_id
                        else:
                            print("❌ Could not retrieve file ID")
                            return None
                    else:
                        response_text = await response.text()
                        print(f"❌ Upload failed: {response.status}, {response_text[:300]}")
        
        except Exception as e:
            print(f"❌ Upload error: {e}")
        
        return None
    
    async def get_latest_file_id(self) -> Optional[str]:
        """Get the latest file ID from dashboard"""
        print("📋 Getting latest file ID from dashboard...")
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        }
        
        try:
            async with self.session.get('https://academi.cx/dashboard', headers=headers) as response:
                if response.status == 200:
                    response_text = await response.text()
                    
                    # Look for the openModal function calls which contain the actual file IDs
                    modal_patterns = [
                        r"openModal\(['\"]([^'\"]*)['\"],\s*['\"]([^'\"]*)['\"]",
                        r"data-file-id=['\"](\d+)['\"]",
                    ]
                    
                    file_ids = []
                    for pattern in modal_patterns:
                        matches = re.findall(pattern, response_text)
                        if pattern == modal_patterns[0]:  # openModal pattern
                            file_ids.extend([match[1] for match in matches if match[1].isdigit()])
                        else:  # data-file-id pattern
                            file_ids.extend(matches)
                    
                    # Remove duplicates while preserving order
                    unique_file_ids = []
                    for fid in file_ids:
                        if fid not in unique_file_ids and len(fid) > 5:  # Filter out short IDs
                            unique_file_ids.append(fid)
                    
                    print(f"Found file IDs: {unique_file_ids}")
                    
                    # Return the first (most recent) file ID
                    if unique_file_ids:
                        latest_id = unique_file_ids[0]
                        print(f"Using latest file ID: {latest_id}")
                        return latest_id
                    
                    # Fallback: look for any numeric IDs that might be file IDs
                    all_numbers = re.findall(r'\b\d{10,}\b', response_text)  # Look for long numbers
                    if all_numbers:
                        fallback_id = all_numbers[0]
                        print(f"Using fallback file ID: {fallback_id}")
                        return fallback_id
        
        except Exception as e:
            print(f"❌ Error getting file ID: {e}")
        
        return None
    
    async def trigger_report_generation(self, file_id: str) -> bool:
        """Trigger report generation by simulating the View Results button click"""
        print(f"🔄 Triggering report generation for file ID: {file_id}")
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': 'https://academi.cx/dashboard',
            'X-Requested-With': 'XMLHttpRequest'
        }
        
        # Try multiple endpoints that might trigger report generation
        trigger_urls = [
            f'https://academi.cx/dashboard/process_file.php?id={file_id}',
            f'https://academi.cx/dashboard/generate_reports.php?id={file_id}',
            f'https://academi.cx/dashboard/check_file.php?id={file_id}',
            f'https://academi.cx/process_file.php?id={file_id}',
            f'https://academi.cx/generate_reports.php?id={file_id}',
        ]
        
        for url in trigger_urls:
            try:
                async with self.session.get(url, headers=headers) as response:
                    if response.status == 200:
                        try:
                            result = await response.json()
                            print(f"✅ Trigger response from {url}: {result}")
                            return True
                        except:
                            response_text = await response.text()
                            if len(response_text) < 1000:
                                print(f"✅ Trigger response from {url}: {response_text[:200]}")
                            if 'success' in response_text.lower() or 'processing' in response_text.lower():
                                return True
            except Exception as e:
                print(f"❌ Error triggering from {url}: {e}")
        
        # If no specific trigger worked, the upload itself might have triggered processing
        print("ℹ️ No specific trigger found, reports should generate automatically after upload")
        return True
    
    async def wait_for_reports(self, file_id: str, max_wait_time: int = 180) -> bool:
        """Wait for AI and similarity reports to be generated (3 minutes max)"""
        print(f"⏳ Waiting for reports to be generated for file ID: {file_id}")
        print("📊 Checking every 20 seconds... (minimum 2 minutes as per your note)")
        
        start_time = time.time()
        check_interval = 20  # Check every 20 seconds
        min_wait_time = 120  # Wait at least 2 minutes as you mentioned
        
        # Initial wait
        print("⏳ Initial wait of 2 minutes for report generation...")
        await asyncio.sleep(min_wait_time)
        
        while time.time() - start_time < max_wait_time:
            elapsed = int(time.time() - start_time)
            minutes = elapsed // 60
            seconds = elapsed % 60
            print(f"⏳ Checking reports... ({minutes}m {seconds}s elapsed)")
            
            # Check if reports are ready
            ai_ready = await self.check_report_ready(file_id, 'ai_report')
            similarity_ready = await self.check_report_ready(file_id, 'similarity_report')
            
            if ai_ready and similarity_ready:
                print("✅ Both AI and similarity reports are ready!")
                return True
            elif ai_ready or similarity_ready:
                ready_type = "AI" if ai_ready else "Similarity"
                print(f"✅ {ready_type} report is ready! Other may still be processing...")
                # Continue waiting for the other report
            else:
                print("📊 Reports still processing...")
            
            await asyncio.sleep(check_interval)
        
        print("⚠️ Timeout waiting for reports, attempting download anyway...")
        return True  # Try download even if timeout
    
    async def check_report_ready(self, file_id: str, report_type: str) -> bool:
        """Check if a specific report is ready for download"""
        url = f"https://academi.cx/dashboard/download_file.php?type={report_type}&id={file_id}"
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': 'https://academi.cx/dashboard',
        }
        
        try:
            async with self.session.head(url, headers=headers, allow_redirects=True) as response:
                print(f"📊 Checking {report_type}: Status {response.status}")
                
                if response.status == 200:
                    content_type = response.headers.get('content-type', '')
                    content_length = response.headers.get('content-length', '0')
                    
                    # Check if it's a valid file download
                    if ('pdf' in content_type.lower() or 
                        'application' in content_type.lower() or 
                        'octet-stream' in content_type.lower() or
                        int(content_length) > 1000):  # File should be reasonably sized
                        print(f"✅ {report_type} ready (Content: {content_type}, Size: {content_length})")
                        return True
                    else:
                        print(f"❌ {report_type} not ready (Content: {content_type}, Size: {content_length})")
                        return False
                else:
                    print(f"❌ {report_type} not ready (Status: {response.status})")
                    return False
        except Exception as e:
            print(f"❌ Error checking {report_type}: {e}")
            return False
    
    async def download_report(self, file_id: str, report_type: str, download_dir: str) -> Optional[str]:
        """Download a specific report"""
        url = f"https://academi.cx/dashboard/download_file.php?type={report_type}&id={file_id}"
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': 'https://academi.cx/dashboard',
        }
        
        try:
            async with self.session.get(url, headers=headers) as response:
                if response.status == 200:
                    content = await response.read()
                    
                    # Generate filename
                    filename = f"{report_type}_{file_id}.pdf"
                    filepath = os.path.join(download_dir, filename)
                    
                    # Check if content is actually a file (not an HTML error page)
                    if len(content) > 1000 and not content.startswith(b'<!DOCTYPE') and not content.startswith(b'<html'):
                        with open(filepath, 'wb') as f:
                            f.write(content)
                        
                        print(f"✅ Downloaded {report_type}: {filepath} ({len(content)} bytes)")
                        return filepath
                    else:
                        print(f"❌ {report_type} download failed - received HTML instead of file")
                        # Save the HTML for debugging
                        debug_file = os.path.join(download_dir, f"debug_{report_type}_{file_id}.html")
                        with open(debug_file, 'wb') as f:
                            f.write(content)
                        print(f"🔍 Debug HTML saved to: {debug_file}")
                        return None
                else:
                    print(f"❌ Failed to download {report_type}: Status {response.status}")
                    return None
        
        except Exception as e:
            print(f"❌ Error downloading {report_type}: {e}")
            return None
    
    async def download_all_reports(self, file_id: str, download_dir: str) -> list:
        """Download all available reports for a file"""
        print(f"📥 Downloading all reports for file ID: {file_id}")
        
        downloaded_files = []
        report_types = ['ai_report', 'similarity_report']
        
        for report_type in report_types:
            filepath = await self.download_report(file_id, report_type, download_dir)
            if filepath:
                downloaded_files.append(filepath)
        
        return downloaded_files
    
    async def process_file(self, file_path: str, download_dir: str = "reports") -> tuple[bool, list]:
        """Complete workflow: upload, wait, and download reports
        
        Returns:
            tuple: (success: bool, downloaded_files: list)
        """
        print(f"🚀 Processing file: {file_path}")
        
        # Ensure download directory exists
        os.makedirs(download_dir, exist_ok=True)
        
        # Upload file and get file ID
        file_id = await self.upload_file(file_path)
        if not file_id:
            return False, []
        
        print(f"📋 File uploaded with ID: {file_id}")
        
        # Trigger report generation (may not be necessary but doesn't hurt)
        await self.trigger_report_generation(file_id)
        
        # Wait for reports to be generated
        await self.wait_for_reports(file_id)
        
        # Download all available reports
        downloaded_files = await self.download_all_reports(file_id, download_dir)
        
        if downloaded_files:
            print(f"✅ Successfully downloaded {len(downloaded_files)} report(s):")
            for file in downloaded_files:
                print(f"   📄 {os.path.basename(file)}")
            return True, downloaded_files
        else:
            print("❌ No reports were downloaded")
            return False, []

    async def close(self):
        """Close the session"""
        if self.session:
            await self.session.close()

# Main function to check file and return results
async def check_file_turnitin(file_path: str, username: str, password: str) -> tuple[bool, str, list]:
    """Main function to check file and return results
    
    Returns:
        tuple: (success: bool, message: str, report_files: list)
    """
    checker = TurnitinChecker(username, password)
    
    try:
        if not await checker.login():
            return False, "❌ Login failed", []
        
        success, downloaded_files = await checker.process_file(file_path)
        
        if success:
            return True, f"✅ File processed successfully! Downloaded {len(downloaded_files)} report(s)", downloaded_files
        else:
            return False, "❌ Failed to process file or download reports", []
    
    except Exception as e:
        return False, f"❌ Error: {str(e)}", []
    
    finally:
        await checker.close()

class RateLimiter:
    def __init__(self):
        # Store user_id -> list of timestamps
        self.user_uploads: Dict[int, List[datetime]] = {}
    
    def can_upload(self, user_id: int) -> Tuple[bool, int]:
        """Check if user can upload. Returns (can_upload, files_used_in_period)"""
        now = datetime.now()
        cutoff_time = now - timedelta(hours=RATE_LIMIT_HOURS)
        
        # Get user's upload history
        if user_id not in self.user_uploads:
            self.user_uploads[user_id] = []
        
        # Remove old uploads
        self.user_uploads[user_id] = [
            upload_time for upload_time in self.user_uploads[user_id]
            if upload_time > cutoff_time
        ]
        
        files_used = len(self.user_uploads[user_id])
        can_upload = files_used < MAX_FILES_PER_PERIOD
        
        return can_upload, files_used
    
    def record_upload(self, user_id: int):
        """Record a successful upload"""
        if user_id not in self.user_uploads:
            self.user_uploads[user_id] = []
        
        self.user_uploads[user_id].append(datetime.now())
    
    def time_until_reset(self, user_id: int) -> timedelta:
        """Get time until user can upload again"""
        if user_id not in self.user_uploads or not self.user_uploads[user_id]:
            return timedelta(0)
        
        oldest_upload = min(self.user_uploads[user_id])
        reset_time = oldest_upload + timedelta(hours=RATE_LIMIT_HOURS)
        now = datetime.now()
        
        if reset_time <= now:
            return timedelta(0)
        
        return reset_time - now

# Global rate limiter instance
rate_limiter = RateLimiter()

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    welcome_message = """
🤖 **Welcome to PDF Plagiarism Checker Bot!**

📄 **How to use:**
1. Send me a PDF file
2. Wait for the analysis to complete (usually 2-3 minutes)
3. Receive your plagiarism and AI detection reports

📊 **Features:**
• AI content detection
• Similarity/plagiarism checking
• Detailed reports in PDF format

⏱️ **Limits:**
• 3 files per 6 hours
• Maximum file size: 20MB
• Supported format: PDF only

Just send me a PDF file to get started! 🚀
"""
    
    await update.message.reply_text(welcome_message, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    help_text = """
🔧 **Commands:**
/start - Start the bot
/help - Show this help message
/status - Check your usage status

📤 **How to check a PDF:**
1. Send me a PDF file directly in the chat
2. I'll analyze it for plagiarism and AI content
3. You'll receive detailed reports

⚠️ **Important Notes:**
• Only PDF files are supported
• Maximum file size: 20MB
• Processing takes 2-3 minutes
• You can check 3 files every 6 hours

🆘 **Having issues?** Make sure your file is:
• In PDF format
• Under 20MB in size
• Not password protected
"""
    
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command"""
    user_id = update.effective_user.id
    can_upload, files_used = rate_limiter.can_upload(user_id)
    
    if can_upload:
        remaining = MAX_FILES_PER_PERIOD - files_used
        status_text = f"""
📊 **Your Status:**

✅ You can upload files!
📈 Files used in last {RATE_LIMIT_HOURS} hours: {files_used}/{MAX_FILES_PER_PERIOD}
🔄 Remaining uploads: {remaining}

Send me a PDF file to analyze! 📄
"""
    else:
        time_until_reset = rate_limiter.time_until_reset(user_id)
        hours = int(time_until_reset.total_seconds() // 3600)
        minutes = int((time_until_reset.total_seconds() % 3600) // 60)
        
        status_text = f"""
📊 **Your Status:**

❌ Upload limit reached
📈 Files used: {files_used}/{MAX_FILES_PER_PERIOD}
⏰ Reset in: {hours}h {minutes}m

You can upload more files after the reset time.
"""
    
    await update.message.reply_text(status_text, parse_mode='Markdown')

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle document uploads"""
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "User"
    
    document: Document = update.message.document
    
    # Check if it's a PDF
    if not document.file_name.lower().endswith('.pdf'):
        await update.message.reply_text(
            "❌ **Only PDF files are supported!**\n\n"
            "Please send a PDF file for analysis.",
            parse_mode='Markdown'
        )
        return
    
    # Check file size
    if document.file_size > MAX_FILE_SIZE:
        size_mb = document.file_size / (1024 * 1024)
        await update.message.reply_text(
            f"❌ **File too large!**\n\n"
            f"Your file: {size_mb:.1f}MB\n"
            f"Maximum allowed: {MAX_FILE_SIZE / (1024 * 1024):.0f}MB\n\n"
            f"Please compress your PDF or send a smaller file.",
            parse_mode='Markdown'
        )
        return
    
    # Check rate limiting
    can_upload, files_used = rate_limiter.can_upload(user_id)
    if not can_upload:
        time_until_reset = rate_limiter.time_until_reset(user_id)
        hours = int(time_until_reset.total_seconds() // 3600)
        minutes = int((time_until_reset.total_seconds() % 3600) // 60)
        
        await update.message.reply_text(
            f"⏱️ **Upload limit reached!**\n\n"
            f"You've used all {MAX_FILES_PER_PERIOD} uploads for this {RATE_LIMIT_HOURS}-hour period.\n"
            f"⏰ Next upload available in: {hours}h {minutes}m\n\n"
            f"Use /status to check your current limits.",
            parse_mode='Markdown'
        )
        return
    
    # Send initial processing message
    processing_message = await update.message.reply_text(
        f"📄 **Processing your PDF...**\n\n"
        f"📁 File: `{document.file_name}`\n"
        f"📊 Size: {document.file_size / 1024:.1f} KB\n"
        f"🔄 Status: Downloading...\n\n"
        f"⏱️ This will take 2-3 minutes. Please wait...",
        parse_mode='Markdown'
    )
    
    # Show typing action
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    
    temp_dir = None
    try:
        # Create temporary directory for this processing
        temp_dir = tempfile.mkdtemp()
        
        # Download the file
        file = await document.get_file()
        file_path = os.path.join(temp_dir, document.file_name)
        await file.download_to_drive(file_path)
        
        # Update status
        await processing_message.edit_text(
            f"📄 **Processing your PDF...**\n\n"
            f"📁 File: `{document.file_name}`\n"
            f"📊 Size: {document.file_size / 1024:.1f} KB\n"
            f"🔄 Status: Analyzing content...\n\n"
            f"⏱️ This will take 2-3 minutes. Please wait...",
            parse_mode='Markdown'
        )
        
        logger.info(f"User {user_name} ({user_id}) uploaded: {document.file_name}")
        
        # Process the file with your academia.cx checker
        success, message, report_files = await check_file_turnitin(
            file_path, ACADEMI_USERNAME, ACADEMI_PASSWORD
        )
        
        if success and report_files:
            # Record successful upload for rate limiting
            rate_limiter.record_upload(user_id)
            
            # Update status to sending reports
            await processing_message.edit_text(
                f"✅ **Analysis Complete!**\n\n"
                f"📁 File: `{document.file_name}`\n"
                f"📊 Reports generated: {len(report_files)}\n"
                f"🔄 Status: Sending reports...",
                parse_mode='Markdown'
            )
            
            # Send each report file
            for i, report_file in enumerate(report_files, 1):
                try:
                    # Determine report type from filename
                    if 'ai_report' in os.path.basename(report_file):
                        report_type = "🤖 AI Detection Report"
                    elif 'similarity_report' in os.path.basename(report_file):
                        report_type = "📊 Similarity Report"
                    else:
                        report_type = f"📄 Report {i}"
                    
                    with open(report_file, 'rb') as f:
                        await context.bot.send_document(
                            chat_id=update.effective_chat.id,
                            document=f,
                            filename=f"{report_type}_{document.file_name}",
                            caption=f"{report_type}\n📁 Original file: `{document.file_name}`",
                            parse_mode='Markdown'
                        )
                    
                    logger.info(f"Sent report {i}/{len(report_files)} to user {user_id}")
                    
                except Exception as e:
                    logger.error(f"Error sending report {report_file}: {e}")
                    await update.message.reply_text(f"❌ Error sending {report_type}")
            
            # Send completion message
            remaining_uploads = MAX_FILES_PER_PERIOD - (files_used + 1)
            completion_text = f"""
🎉 **Analysis Complete!**

✅ Successfully processed: `{document.file_name}`
📊 Reports sent: {len(report_files)}

📈 **Usage Status:**
• Files used today: {files_used + 1}/{MAX_FILES_PER_PERIOD}
• Remaining uploads: {remaining_uploads}

Thank you for using our service! 🚀
"""
            
            await processing_message.edit_text(completion_text, parse_mode='Markdown')
            
        else:
            # Processing failed
            await processing_message.edit_text(
                f"❌ **Processing Failed**\n\n"
                f"📁 File: `{document.file_name}`\n"
                f"🔄 Error: {message}\n\n"
                f"Please try again later or contact support if the issue persists.",
                parse_mode='Markdown'
            )
            logger.error(f"Processing failed for user {user_id}: {message}")
    
    except Exception as e:
        logger.error(f"Error processing file for user {user_id}: {e}")
        
        try:
            await processing_message.edit_text(
                f"❌ **An error occurred**\n\n"
                f"📁 File: `{document.file_name}`\n"
                f"🔄 Error: {str(e)}\n\n"
                f"Please try again later.",
                parse_mode='Markdown'
            )
        except:
            await update.message.reply_text(
                f"❌ **An error occurred while processing your file.**\n\n"
                f"Please try again later."
            )
    
    finally:
        # Cleanup temporary files
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception as e:
                logger.error(f"Error cleaning up temp dir {temp_dir}: {e}")

async def handle_non_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle non-document messages"""
    await update.message.reply_text(
        "📄 **Please send a PDF file for analysis.**\n\n"
        "I can only process PDF documents. Send me a PDF file and I'll check it for plagiarism and AI content!\n\n"
        "Use /help for more information.",
        parse_mode='Markdown'
    )

def main():
    """Start the bot"""
    print("🤖 Starting Telegram Bot...")
    
    # Start Flask app in a separate thread
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print(f"🌐 Flask server started on port {PORT}")
    
    # Start keep-alive pinger
    if RENDER_URL:
        print("🏓 Starting keep-alive pinger...")
        Timer(300, keep_alive).start()  # Start first ping after 5 minutes
    
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    
    # Handle document uploads
    application.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    
    # Handle non-document messages
    application.add_handler(MessageHandler(~filters.Document.ALL, handle_non_document))
    
    print("✅ Bot started successfully!")
    print("🔍 Waiting for users to send PDF files...")
    
    # Start polling
    application.run_polling(allowed_updates=Update.ALL_TYPES)

# Quick test function for standalone testing
async def quick_test(file_path: str) -> tuple[bool, list]:
    """Quick test function that returns success status and downloaded files"""
    success, message, files = await check_file_turnitin(file_path, ACADEMI_USERNAME, ACADEMI_PASSWORD)
    print(message)
    return success, files

# Test the enhanced functionality (for development only)
async def test_checker():
    """Test function for development - remove in production"""
    print("🚀 Testing Turnitin Checker")
    print("=" * 50)
    
    test_file = "test.pdf"  # Replace with your test file
    
    if os.path.exists(test_file):
        success, downloaded_files = await quick_test(test_file)
        
        if success:
            print(f"\n🎉 SUCCESS! Downloaded files:")
            for file in downloaded_files:
                print(f"   📄 {file}")
        else:
            print(f"\n❌ Test failed")
    else:
        print(f"⚠️ Test file not found: {test_file}")
        print("Please create a test PDF file to test the functionality")

if __name__ == "__main__":
    import sys
    
    # Check if running in test mode
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        asyncio.run(test_checker())
    else:
        # Start the Telegram bot
        main()