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
import uuid
from dataclasses import dataclass

from telegram import Update, Document
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ChatAction

# Keep-alive server (Render Web Service needs to bind to $PORT even if we're polling)
from flask import Flask
import requests
from threading import Timer

# -------------------------------------------------------------------
# Logging
# -------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# Config (env or defaults — you said plain is OK)
# -------------------------------------------------------------------
ACADEMI_USERNAME = os.getenv("ACADEMI_USERNAME", "mazbhaulhaque@gmail.com")
ACADEMI_PASSWORD = os.getenv("ACADEMI_PASSWORD", "Tuistnig23ZL.pb")
BOT_TOKEN = os.getenv("BOT_TOKEN", "7504576229:AAHFIJSD_ZLQv95XVgSBe-SOOoO5JkacsOE")

# Render / Flask
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_URL", "")

# Limits
MAX_FILES_PER_PERIOD = 3           # default/free users
RATE_LIMIT_HOURS = 6
MAX_FILE_SIZE = 20 * 1024 * 1024   # 20MB

# Concurrency
MAX_CONCURRENT_SESSIONS = 20
MAX_CONCURRENT_PER_USER = 3
MAX_PROCESSING_QUEUE = 50

# -------------------------------------------------------------------
# Premium feature
# -------------------------------------------------------------------
PREMIUM_CODE = "OpenCvA1@slr"
PREMIUM_FILES_PER_PERIOD = 15
PREMIUM_DB_FILE = "premium_users.json"

def load_premium_users() -> set[int]:
    try:
        if os.path.exists(PREMIUM_DB_FILE):
            with open(PREMIUM_DB_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
    except Exception as e:
        logger.error(f"Failed to load premium users: {e}")
    return set()

def save_premium_users(users: set[int]):
    try:
        with open(PREMIUM_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(list(users), f)
    except Exception as e:
        logger.error(f"Failed to save premium users: {e}")

PREMIUM_USERS = load_premium_users()

def is_premium(user_id: int) -> bool:
    return user_id in PREMIUM_USERS

# -------------------------------------------------------------------
# Flask keep-alive (for Render Web Service health checks)
# -------------------------------------------------------------------
app = Flask(__name__)

@app.route('/')
def home():
    return "Telegram Bot is running!"

@app.route('/health')
def health():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

def keep_alive():
    """Ping the service periodically (optional)"""
    if RENDER_URL:
        try:
            response = requests.get(f"{RENDER_URL}/health", timeout=10)
            logger.info(f"Keep-alive ping successful: {response.status_code}")
        except Exception as e:
            logger.error(f"Keep-alive ping failed: {e}")
    Timer(840, keep_alive).start()  # 14 minutes

def run_flask():
    app.run(host='0.0.0.0', port=PORT, debug=False)

# -------------------------------------------------------------------
# Processing structures
# -------------------------------------------------------------------
@dataclass
class ProcessingJob:
    user_id: int
    user_name: str
    file_path: str
    filename: str
    file_size: int
    temp_dir: str
    processing_message: Any
    context: ContextTypes.DEFAULT_TYPE
    chat_id: int
    job_id: str
    created_at: datetime

class ConcurrentProcessingManager:
    def __init__(self):
        self.processing_queue = asyncio.Queue(maxsize=MAX_PROCESSING_QUEUE)
        self.active_jobs: Dict[str, ProcessingJob] = {}
        self.user_active_jobs: Dict[int, List[str]] = {}
        self.session_pool = []
        self.session_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)
        self._lock = asyncio.Lock()
        self.workers_started = False

    async def start_workers(self, num_workers: int = 5):
        if self.workers_started:
            return
        logger.info(f"Starting {num_workers} processing workers...")
        for i in range(num_workers):
            asyncio.create_task(self._worker(f"worker-{i}"))
        self.workers_started = True
        logger.info(f"✅ {num_workers} workers started successfully")

    async def add_job(self, job: ProcessingJob) -> bool:
        try:
            async with self._lock:
                user_jobs = self.user_active_jobs.get(job.user_id, [])
                if len(user_jobs) >= MAX_CONCURRENT_PER_USER:
                    return False
                self.active_jobs[job.job_id] = job
                if job.user_id not in self.user_active_jobs:
                    self.user_active_jobs[job.user_id] = []
                self.user_active_jobs[job.user_id].append(job.job_id)
            await self.processing_queue.put(job)
            logger.info(f"Job {job.job_id} added to queue for user {job.user_id}")
            return True
        except asyncio.QueueFull:
            async with self._lock:
                if job.job_id in self.active_jobs:
                    del self.active_jobs[job.job_id]
                if job.user_id in self.user_active_jobs:
                    if job.job_id in self.user_active_jobs[user_id]:
                        self.user_active_jobs[user_id].remove(job.job_id)
                    if not self.user_active_jobs[user_id]:
                        del self.user_active_jobs[user_id]
            return False

    async def _worker(self, worker_name: str):
        logger.info(f"Worker {worker_name} started")
        while True:
            try:
                job = await self.processing_queue.get()
                logger.info(f"Worker {worker_name} picked up job {job.job_id} for user {job.user_id}")
                await self._process_job(job, worker_name)
                self.processing_queue.task_done()
            except Exception as e:
                logger.error(f"Worker {worker_name} error: {e}")
                self.processing_queue.task_done()

    async def _process_job(self, job: ProcessingJob, worker_name: str):
        session_id = None
        try:
            await self._update_job_status(job, "🔄 Processing started...", worker_name)
            await self.session_semaphore.acquire()
            session_id = str(uuid.uuid4())
            logger.info(f"Worker {worker_name} acquired session {session_id[:8]} for job {job.job_id}")

            await self._update_job_status(job, "🔍 Analyzing document...", worker_name)

            success, message, report_files = await check_file_turnitin(
                job.file_path, ACADEMI_USERNAME, ACADEMI_PASSWORD, job.user_id, session_id
            )

            if success and report_files:
                await self._send_reports(job, report_files, worker_name)
                await rate_limiter.record_upload(job.user_id)
            else:
                await self._send_error(job, message, worker_name)

        except Exception as e:
            logger.error(f"Worker {worker_name} failed to process job {job.job_id}: {e}")
            await self._send_error(job, f"Processing error: {str(e)}", worker_name)

        finally:
            if session_id:
                self.session_semaphore.release()
            await self._cleanup_job(job)

    async def _update_job_status(self, job: ProcessingJob, status: str, worker_name: str):
        try:
            status_text = f"""
📄 **Processing your PDF...**

📁 File: `{job.filename}`
📊 Size: {job.file_size / 1024:.1f} KB
🔄 Status: {status}
⚡ Worker: {worker_name}

⏱️ Please wait...
"""
            await job.processing_message.edit_text(status_text, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Failed to update status for job {job.job_id}: {e}")

    async def _send_reports(self, job: ProcessingJob, report_files: List[str], worker_name: str):
        try:
            await self._update_job_status(job, f"✅ Sending {len(report_files)} reports...", worker_name)
            for i, report_file in enumerate(report_files, 1):
                try:
                    if 'ai_report' in os.path.basename(report_file):
                        report_type = "🤖 AI Detection Report"
                    elif 'similarity_report' in os.path.basename(report_file):
                        report_type = "📊 Similarity Report"
                    else:
                        report_type = f"📄 Report {i}"

                    with open(report_file, 'rb') as f:
                        await job.context.bot.send_document(
                            chat_id=job.chat_id,
                            document=f,
                            filename=f"{report_type.replace('🤖', 'AI').replace('📊', 'Similarity')}_{job.filename}",
                            caption=f"{report_type}\n📁 Original: `{job.filename}`",
                            parse_mode='Markdown'
                        )
                    logger.info(f"Worker {worker_name} sent report {i}/{len(report_files)} for job {job.job_id}")
                except Exception as e:
                    logger.error(f"Error sending report {report_file}: {e}")

            # dynamic limit for message
            can_upload_again, files_used_now = await rate_limiter.can_upload(job.user_id)
            user_limit = PREMIUM_FILES_PER_PERIOD if is_premium(job.user_id) else MAX_FILES_PER_PERIOD
            remaining_uploads = max(0, user_limit - files_used_now)

            completion_text = f"""
🎉 **Analysis Complete!**

✅ Processed: `{job.filename}`
📊 Reports sent: {len(report_files)}
⚡ Worker: {worker_name}

📈 **Your Usage:**
• Files used in {RATE_LIMIT_HOURS}h: {files_used_now}/{user_limit}
• Remaining: {remaining_uploads}

Thank you! 🚀
"""
            await job.processing_message.edit_text(completion_text, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error sending reports for job {job.job_id}: {e}")

    async def _send_error(self, job: ProcessingJob, error_message: str, worker_name: str):
        try:
            error_text = f"""
❌ **Processing Failed**

📁 File: `{job.filename}`
🔄 Error: {error_message}
⚡ Worker: {worker_name}

Please try again later.
"""
            await job.processing_message.edit_text(error_text, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error sending error message for job {job.job_id}: {e}")

    async def _cleanup_job(self, job: ProcessingJob):
        try:
            async with self._lock:
                if job.job_id in self.active_jobs:
                    del self.active_jobs[job.job_id]
                if job.user_id in self.user_active_jobs:
                    if job.job_id in self.user_active_jobs[job.user_id]:
                        self.user_active_jobs[job.user_id].remove(job.job_id)
                    if not self.user_active_jobs[job.user_id]:
                        del self.user_active_jobs[job.user_id]
            if os.path.exists(job.temp_dir):
                shutil.rmtree(job.temp_dir)
            logger.info(f"Job {job.job_id} cleaned up")
        except Exception as e:
            logger.error(f"Error cleaning up job {job.job_id}: {e}")

    async def get_queue_status(self) -> Dict[str, int]:
        async with self._lock:
            total_active = len(self.active_jobs)
            per_user = {uid: len(jobs) for uid, jobs in self.user_active_jobs.items()}
        return {
            'queue_size': self.processing_queue.qsize(),
            'total_active_jobs': total_active,
            'active_per_user': per_user
        }

class SessionManager:
    def __init__(self, max_sessions: int = MAX_CONCURRENT_SESSIONS):
        self.max_sessions = max_sessions
        self.active_sessions = {}
        self.session_semaphore = asyncio.Semaphore(max_sessions)
        self._lock = asyncio.Lock()

    async def get_session_id(self) -> str:
        return str(uuid.uuid4())

    async def acquire_session(self, session_id: str) -> bool:
        await self.session_semaphore.acquire()
        async with self._lock:
            self.active_sessions[session_id] = datetime.now()
        return True

    async def release_session(self, session_id: str):
        async with self._lock:
            if session_id in self.active_sessions:
                del self.active_sessions[session_id]
        self.session_semaphore.release()

    async def get_active_sessions_count(self) -> int:
        async with self._lock:
            return len(self.active_sessions)

# -------------------------------------------------------------------
# Academi / Turnitin-like checker (with filename-based deconfliction)
# -------------------------------------------------------------------
class TurnitinChecker:
    def __init__(self, username: str, password: str, session_id: str):
        self.username = username
        self.password = password
        self.session_id = session_id
        self.session: Optional[aiohttp.ClientSession] = None
        self.upload_endpoint = "https://academi.cx/dashboard/file_upload.php"
        self.last_uploaded_name: Optional[str] = None  # unique filename we send

    async def login(self) -> bool:
        logger.info(f"🔐 Logging in... (Session: {self.session_id[:8]})")
        connector = aiohttp.TCPConnector(
            limit=20, limit_per_host=10, keepalive_timeout=60,
            enable_cleanup_closed=True, use_dns_cache=True, ttl_dns_cache=300
        )
        timeout = aiohttp.ClientTimeout(total=90, connect=30)
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={
                'User-Agent': f'Mozilla/5.0 Session-{self.session_id[:8]}'
            }
        )
        login_data = {'email': self.username, 'password': self.password, 'rememberme': 'on'}
        headers = {'Referer': 'https://academi.cx/login', 'Content-Type': 'application/x-www-form-urlencoded'}
        try:
            async with self.session.post('https://academi.cx/login/login.php',
                                         data=login_data, headers=headers) as resp:
                txt = await resp.text()
                if resp.status in [200, 302] and ('dashboard' in txt.lower() or resp.status == 302):
                    logger.info(f"✅ Login successful (Session: {self.session_id[:8]})")
                    return True
        except Exception as e:
            logger.error(f"❌ Login error: {e}")
        return False

    async def upload_file(self, file_path: str) -> Optional[str]:
        """Upload with a UNIQUE filename so we can later find the correct file ID."""
        if not os.path.exists(file_path):
            logger.error(f"❌ File not found: {file_path}")
            return None

        unique_name = f"{self.session_id[:8]}__{os.path.basename(file_path)}"
        self.last_uploaded_name = unique_name
        logger.info(f"📤 Uploading as: {unique_name} (Session: {self.session_id[:8]})")

        headers = {'Referer': 'https://academi.cx/dashboard'}
        try:
            with open(file_path, 'rb') as f:
                data = aiohttp.FormData()
                data.add_field('file', f, filename=unique_name)
                async with self.session.post(self.upload_endpoint, data=data, headers=headers) as response:
                    logger.info(f"Upload Status: {response.status} (Session: {self.session_id[:8]})")
                    if response.status == 200:
                        await asyncio.sleep(3)
                        # find the file ID that matches our unique filename
                        file_id = await self.get_latest_file_id(expected_filename=unique_name)
                        if file_id:
                            logger.info(f"📋 Matched file ID {file_id} for {unique_name}")
                            return file_id
                        logger.error("❌ Could not match uploaded file ID")
        except Exception as e:
            logger.error(f"❌ Upload error: {e}")
        return None

    async def _extract_nearby_id(self, html: str, filename: str) -> Optional[str]:
        """Find the openModal()/data-file-id nearest to the filename occurrence."""
        idx = html.find(filename)
        if idx == -1:
            return None
        window = 2000  # look around the filename
        start = max(0, idx - window)
        end = min(len(html), idx + window)

        snippet = html[start:end]

        # Try common patterns inside that neighborhood
        m = re.search(r"openModal\(['\"][^'\"]*['\"],\s*['\"](\d+)['\"]\)", snippet)
        if m:
            return m.group(1)
        m2 = re.search(r"data-file-id=['\"](\d+)['\"]", snippet)
        if m2:
            return m2.group(1)
        # fallback: any longish number nearby
        m3 = re.search(r"\b(\d{6,})\b", snippet)
        if m3:
            return m3.group(1)
        return None

    async def get_latest_file_id(self, expected_filename: Optional[str] = None) -> Optional[str]:
        logger.info(f"📋 Getting file ID (expecting {expected_filename}) (Session: {self.session_id[:8]})")
        try:
            async with self.session.get('https://academi.cx/dashboard') as response:
                if response.status == 200:
                    html = await response.text()

                    # If we know the unique filename, try to get the ID closest to it
                    if expected_filename:
                        fid = await self._extract_nearby_id(html, expected_filename)
                        if fid:
                            return fid

                    # Generic fallback (old behavior)
                    patterns = [
                        r"openModal\(['\"][^'\"]*['\"],\s*['\"](\d+)['\"]",
                        r"data-file-id=['\"](\d+)['\"]",
                    ]
                    for pat in patterns:
                        matches = re.findall(pat, html)
                        if matches:
                            return matches[0]
        except Exception as e:
            logger.error(f"❌ Error getting file ID: {e}")
        return None

    async def trigger_report_generation(self, file_id: str) -> bool:
        logger.info(f"🔄 Triggering report generation for {file_id}")
        headers = {'Referer': 'https://academi.cx/dashboard', 'X-Requested-With': 'XMLHttpRequest'}
        urls = [
            f'https://academi.cx/dashboard/process_file.php?id={file_id}',
            f'https://academi.cx/dashboard/generate_reports.php?id={file_id}',
            f'https://academi.cx/dashboard/check_file.php?id={file_id}',
        ]
        for url in urls:
            try:
                async with self.session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                pass
        return True

    async def wait_for_reports(self, file_id: str, max_wait_time: int = 180) -> bool:
        logger.info(f"⏳ Waiting for reports for {file_id}")
        start = time.time()
        check_interval = 20
        min_wait = 120
        await asyncio.sleep(min_wait)
        while time.time() - start < max_wait_time:
            ai_ready = await self.check_report_ready(file_id, 'ai_report')
            sim_ready = await self.check_report_ready(file_id, 'similarity_report')
            if ai_ready and sim_ready:
                return True
            await asyncio.sleep(check_interval)
        return True

    async def check_report_ready(self, file_id: str, report_type: str) -> bool:
        url = f"https://academi.cx/dashboard/download_file.php?type={report_type}&id={file_id}"
        try:
            async with self.session.head(url, allow_redirects=True) as resp:
                if resp.status == 200:
                    ct = resp.headers.get('content-type', '')
                    cl = resp.headers.get('content-length', '0')
                    if ('pdf' in ct.lower() or 'application' in ct.lower() or int(cl or 0) > 1000):
                        return True
        except Exception:
            pass
        return False

    async def download_report(self, file_id: str, report_type: str, download_dir: str) -> Optional[str]:
        url = f"https://academi.cx/dashboard/download_file.php?type={report_type}&id={file_id}"
        try:
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    content = await resp.read()
                    filename = f"{report_type}_{file_id}_{self.session_id[:8]}.pdf"
                    path = os.path.join(download_dir, filename)
                    if len(content) > 1000 and not content.startswith(b'<!DOCTYPE'):
                        with open(path, 'wb') as f:
                            f.write(content)
                        return path
        except Exception as e:
            logger.error(f"❌ Error downloading {report_type}: {e}")
        return None

    async def download_all_reports(self, file_id: str, download_dir: str) -> list:
        os.makedirs(download_dir, exist_ok=True)
        tasks = [
            self.download_report(file_id, 'ai_report', download_dir),
            self.download_report(file_id, 'similarity_report', download_dir)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if isinstance(r, str) and r]

    async def process_file(self, file_path: str, download_dir: str = "reports") -> tuple[bool, list]:
        file_id = await self.upload_file(file_path)
        if not file_id:
            return False, []
        await self.trigger_report_generation(file_id)
        await self.wait_for_reports(file_id)
        files = await self.download_all_reports(file_id, download_dir)
        return (len(files) > 0), files

    async def close(self):
        if self.session:
            try:
                await self.session.close()
            except Exception as e:
                logger.error(f"Error closing session: {e}")

# -------------------------------------------------------------------
# Rate Limiter (dynamic per-user limits)
# -------------------------------------------------------------------
class RateLimiter:
    def __init__(self):
        self.user_uploads: Dict[int, List[datetime]] = {}
        self.active_processing: Dict[int, int] = {}
        self._lock = asyncio.Lock()

    async def can_upload(self, user_id: int) -> Tuple[bool, int]:
        async with self._lock:
            now = datetime.now()
            cutoff = now - timedelta(hours=RATE_LIMIT_HOURS)

            if user_id not in self.user_uploads:
                self.user_uploads[user_id] = []

            self.user_uploads[user_id] = [t for t in self.user_uploads[user_id] if t > cutoff]
            files_used = len(self.user_uploads[user_id])
            active_count = self.active_processing.get(user_id, 0)

            user_limit = PREMIUM_FILES_PER_PERIOD if is_premium(user_id) else MAX_FILES_PER_PERIOD

            can_upload = (files_used < user_limit and active_count < MAX_CONCURRENT_PER_USER)
            return can_upload, files_used

    async def record_upload(self, user_id: int):
        async with self._lock:
            self.user_uploads.setdefault(user_id, []).append(datetime.now())

    async def start_processing(self, user_id: int):
        async with self._lock:
            self.active_processing[user_id] = self.active_processing.get(user_id, 0) + 1

    async def finish_processing(self, user_id: int):
        async with self._lock:
            if user_id in self.active_processing:
                self.active_processing[user_id] = max(0, self.active_processing[user_id] - 1)
                if self.active_processing[user_id] == 0:
                    del self.active_processing[user_id]

    async def time_until_reset(self, user_id: int) -> timedelta:
        async with self._lock:
            if user_id not in self.user_uploads or not self.user_uploads[user_id]:
                return timedelta(0)
            oldest = min(self.user_uploads[user_id])
            reset_time = oldest + timedelta(hours=RATE_LIMIT_HOURS)
            now = datetime.now()
            return timedelta(0) if reset_time <= now else (reset_time - now)

# Globals
rate_limiter = RateLimiter()
session_manager = SessionManager()
processing_manager = ConcurrentProcessingManager()

# -------------------------------------------------------------------
# Core pipeline
# -------------------------------------------------------------------
async def check_file_turnitin(file_path: str, username: str, password: str, user_id: int, session_id: str = None) -> tuple[bool, str, list]:
    if not session_id:
        session_id = await session_manager.get_session_id()

    checker = None
    try:
        await session_manager.acquire_session(session_id)
        logger.info(f"Session acquired: {session_id[:8]} for user {user_id}")
        checker = TurnitinChecker(username, password, session_id)

        if not await checker.login():
            return False, "❌ Login failed", []

        download_dir = f"reports/user_{user_id}_{session_id[:8]}"
        success, downloaded_files = await checker.process_file(file_path, download_dir)
        if success:
            return True, f"✅ File processed successfully! Downloaded {len(downloaded_files)} report(s)", downloaded_files
        else:
            return False, "❌ Failed to process file or download reports", []

    except Exception as e:
        logger.error(f"Error processing file for user {user_id} (Session: {session_id[:8]}) : {e}")
        return False, f"❌ Error: {str(e)}", []
    finally:
        if checker:
            try:
                await checker.close()
            except Exception:
                pass
        if session_id:
            await session_manager.release_session(session_id)
            logger.info(f"Session released: {session_id[:8]} for user {user_id}")

# -------------------------------------------------------------------
# Commands / Handlers
# -------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active_sessions = await session_manager.get_active_sessions_count()
    queue_status = await processing_manager.get_queue_status()
    user_id = update.effective_user.id
    user_limit = PREMIUM_FILES_PER_PERIOD if is_premium(user_id) else MAX_FILES_PER_PERIOD

    welcome_message = f"""
🤖 **Welcome to PDF Plagiarism Checker Bot!**

📄 **How to use:**
1. Send me a PDF file
2. Your file will be processed concurrently
3. Receive your reports in 2-3 minutes

📊 **Features:**
• AI content detection
• Similarity/plagiarism checking
• Concurrent processing for multiple users
• Real-time status updates

⏱️ **Limits:**
• {user_limit} files per {RATE_LIMIT_HOURS} hours
• Maximum file size: {MAX_FILE_SIZE // (1024*1024)}MB
• Max {MAX_CONCURRENT_PER_USER} files processing simultaneously per user
• Supported format: PDF only

⭐ **Premium:** `/premium OpenCvA1@slr` → 15 files / 6h

📈 **System Status:**
• Active sessions: {active_sessions}/{MAX_CONCURRENT_SESSIONS}
• Queue size: {queue_status['queue_size']}
• Total active jobs: {queue_status['total_active_jobs']}
"""
    await update.message.reply_text(welcome_message, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = f"""
🔧 **Commands:**
/start - Start the bot
/help - Show this help message
/status - Check your usage status
/premium <code> - Unlock premium (15 files / 6h)

📤 **How to check a PDF:**
1. Send a PDF
2. I queue & process it
3. You receive the AI + similarity reports

⚠️ **Notes:**
• Only PDF, under {MAX_FILE_SIZE // (1024*1024)}MB
• Processing ~2-3 minutes
• Default limit: {MAX_FILES_PER_PERIOD} per {RATE_LIMIT_HOURS}h (Premium 15)
"""
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_limit = PREMIUM_FILES_PER_PERIOD if is_premium(user_id) else MAX_FILES_PER_PERIOD
    can_upload, files_used = await rate_limiter.can_upload(user_id)
    active_sessions = await session_manager.get_active_sessions_count()
    queue_status = await processing_manager.get_queue_status()
    user_active_jobs = queue_status['active_per_user'].get(user_id, 0)

    if can_upload:
        remaining = max(0, user_limit - files_used)
        status_text = f"""
📊 **Your Status:**

✅ You can upload files!
📈 Files used in last {RATE_LIMIT_HOURS} hours: {files_used}/{user_limit}
🔄 Remaining uploads: {remaining}
⚡ Your active jobs: {user_active_jobs}/{MAX_CONCURRENT_PER_USER}

📈 **System Status:**
🖥️ Active sessions: {active_sessions}/{MAX_CONCURRENT_SESSIONS}
📋 Global queue size: {queue_status['queue_size']}
⚡ Total active jobs: {queue_status['total_active_jobs']}
"""
    else:
        time_until_reset = await rate_limiter.time_until_reset(user_id)
        hours = int(time_until_reset.total_seconds() // 3600)
        minutes = int((time_until_reset.total_seconds() % 3600) // 60)
        reason = (
            f"❌ Upload limit reached ({files_used}/{user_limit})"
            if files_used >= user_limit else
            f"⏳ Too many files processing ({user_active_jobs}/{MAX_CONCURRENT_PER_USER})"
        )
        status_text = f"""
📊 **Your Status:**

{reason}
📈 Files used: {files_used}/{user_limit}
⚡ Your active jobs: {user_active_jobs}/{MAX_CONCURRENT_PER_USER}
⏰ Reset in: {hours}h {minutes}m

📈 **System Status:**
🖥️ Active sessions: {active_sessions}/{MAX_CONCURRENT_SESSIONS}
📋 Global queue size: {queue_status['queue_size']}
⚡ Total active jobs: {queue_status['total_active_jobs']}
"""
    await update.message.reply_text(status_text, parse_mode='Markdown')

async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args if hasattr(context, "args") else []
    if not args:
        await update.message.reply_text(
            "⭐ *Premium Unlock*\n\n"
            "Send the code like this:\n"
            "`/premium OpenCvA1@slr`",
            parse_mode='Markdown'
        )
        return
    code = args[0].strip()
    if code == PREMIUM_CODE:
        if is_premium(user_id):
            await update.message.reply_text("✅ You already have *Premium* (15 files / 6h).", parse_mode='Markdown')
            return
        PREMIUM_USERS.add(user_id)
        save_premium_users(PREMIUM_USERS)
        await update.message.reply_text(
            "🎉 *Premium activated!*\n\nYou can now upload *15 files every 6 hours*.",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("❌ Invalid code. Please check and try again.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "User"
    document: Document = update.message.document

    if not document.file_name.lower().endswith('.pdf'):
        await update.message.reply_text(
            "❌ **Only PDF files are supported!**",
            parse_mode='Markdown'
        )
        return

    if document.file_size > MAX_FILE_SIZE:
        size_mb = document.file_size / (1024 * 1024)
        await update.message.reply_text(
            f"❌ **File too large!** Your file: {size_mb:.1f}MB (max {MAX_FILE_SIZE / (1024 * 1024):.0f}MB).",
            parse_mode='Markdown'
        )
        return

    user_limit = PREMIUM_FILES_PER_PERIOD if is_premium(user_id) else MAX_FILES_PER_PERIOD
    can_upload, files_used = await rate_limiter.can_upload(user_id)
    if not can_upload:
        queue_status = await processing_manager.get_queue_status()
        user_active_jobs = queue_status['active_per_user'].get(user_id, 0)
        if user_active_jobs >= MAX_CONCURRENT_PER_USER:
            await update.message.reply_text(
                f"⏳ **Too many files processing!** ({user_active_jobs}/{MAX_CONCURRENT_PER_USER}).",
                parse_mode='Markdown'
            )
            return
        time_until_reset = await rate_limiter.time_until_reset(user_id)
        hours = int(time_until_reset.total_seconds() // 3600)
        minutes = int((time_until_reset.total_seconds() % 3600) // 60)
        await update.message.reply_text(
            f"⏱️ **Upload limit reached!**\n"
            f"You've used all {user_limit} uploads for this {RATE_LIMIT_HOURS}-hour period.\n"
            f"⏰ Next upload in: {hours}h {minutes}m\n\nUse /status to check your limits.",
            parse_mode='Markdown'
        )
        return

    queue_status = await processing_manager.get_queue_status()
    if queue_status['queue_size'] >= MAX_PROCESSING_QUEUE:
        await update.message.reply_text(
            f"🔄 **System queue full!** Please try again in a few minutes.",
            parse_mode='Markdown'
        )
        return

    temp_dir = tempfile.mkdtemp(prefix=f"user_{user_id}_")
    try:
        file = await document.get_file()
        file_path = os.path.join(temp_dir, document.file_name)
        await file.download_to_drive(file_path)

        processing_message = await update.message.reply_text(
            f"📄 **File Queued for Processing!**\n"
            f"📁 `{document.file_name}`\n"
            f"📋 Queue position: {queue_status['queue_size'] + 1}",
            parse_mode='Markdown'
        )

        job = ProcessingJob(
            user_id=user_id,
            user_name=user_name,
            file_path=file_path,
            filename=document.file_name,
            file_size=document.file_size,
            temp_dir=temp_dir,
            processing_message=processing_message,
            context=context,
            chat_id=update.effective_chat.id,
            job_id=str(uuid.uuid4()),
            created_at=datetime.now()
        )

        success = await processing_manager.add_job(job)
        if success:
            await processing_message.edit_text(
                f"📄 **Queued!** `{document.file_name}`\n"
                f"🆔 Job ID: `{job.job_id[:8]}`\n"
                f"⏱️ Processing will begin automatically.",
                parse_mode='Markdown'
            )
            logger.info(f"✅ Queued file for user {user_name} ({user_id}) Job {job.job_id[:8]}")
        else:
            await processing_message.edit_text(
                f"❌ **Failed to queue file.** Please try again later.",
                parse_mode='Markdown'
            )
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)

    except Exception as e:
        logger.error(f"Error handling document for user {user_id}: {e}")
        try:
            await update.message.reply_text(
                f"❌ **Error queuing file**\n`{document.file_name}`\n{str(e)}",
                parse_mode='Markdown'
            )
        except:
            pass
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

async def handle_non_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    queue_status = await processing_manager.get_queue_status()
    await update.message.reply_text(
        f"📄 **Please send a PDF file for analysis.**\n"
        f"Current queue: {queue_status['queue_size']} file(s)\n\nUse /help for details.",
        parse_mode='Markdown'
    )

# -------------------------------------------------------------------
# App bootstrap
# -------------------------------------------------------------------
def main():
    print("🤖 Starting Multi-User Concurrent Telegram Bot v2.0...")
    # Start Flask server (Render requires web binding)
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print(f"🌐 Flask server started on port {PORT}")

    if RENDER_URL:
        print("🏓 Starting keep-alive pinger...")
        Timer(300, keep_alive).start()

    application = Application.builder().token(BOT_TOKEN).build()

    # Commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("premium", premium_command))

    # Documents and fallback
    application.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    application.add_handler(MessageHandler(~filters.Document.ALL, handle_non_document))

    async def startup_callback(app):
        await processing_manager.start_workers(num_workers=8)
        print("🔧 Processing workers started successfully")

    application.post_init = startup_callback

    print("✅ Bot started successfully with TRUE concurrent processing!")
    print(f"📊 Max concurrent sessions: {MAX_CONCURRENT_SESSIONS}")
    print(f"👥 Max concurrent per user: {MAX_CONCURRENT_PER_USER}")
    print(f"📋 Max processing queue: {MAX_PROCESSING_QUEUE}")
    print(f"⚙️ Processing workers: 8")

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        # Optional: you can keep your test harness if you want
        pass
    else:
        main()
