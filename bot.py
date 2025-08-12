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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

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

# Concurrency limits - Increased for better performance
MAX_CONCURRENT_SESSIONS = 20  # Maximum concurrent academi.cx sessions
MAX_CONCURRENT_PER_USER = 3   # Maximum concurrent requests per user
MAX_PROCESSING_QUEUE = 50     # Maximum items in processing queue

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

@dataclass
class ProcessingJob:
    """Represents a file processing job"""
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
    """Manages concurrent processing of files for multiple users"""
    
    def __init__(self):
        self.processing_queue = asyncio.Queue(maxsize=MAX_PROCESSING_QUEUE)
        self.active_jobs: Dict[str, ProcessingJob] = {}
        self.user_active_jobs: Dict[int, List[str]] = {}
        self.session_pool = []
        self.session_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)
        self._lock = asyncio.Lock()
        self.workers_started = False
        
    async def start_workers(self, num_workers: int = 5):
        """Start worker tasks to process the queue"""
        if self.workers_started:
            return
            
        logger.info(f"Starting {num_workers} processing workers...")
        
        # Start worker tasks
        for i in range(num_workers):
            asyncio.create_task(self._worker(f"worker-{i}"))
        
        self.workers_started = True
        logger.info(f"✅ {num_workers} workers started successfully")
    
    async def add_job(self, job: ProcessingJob) -> bool:
        """Add a job to the processing queue"""
        try:
            # Check user's active jobs
            async with self._lock:
                user_jobs = self.user_active_jobs.get(job.user_id, [])
                if len(user_jobs) >= MAX_CONCURRENT_PER_USER:
                    return False
                
                # Add to tracking
                self.active_jobs[job.job_id] = job
                if job.user_id not in self.user_active_jobs:
                    self.user_active_jobs[job.user_id] = []
                self.user_active_jobs[job.user_id].append(job.job_id)
            
            # Add to queue (this will block if queue is full)
            await self.processing_queue.put(job)
            logger.info(f"Job {job.job_id} added to queue for user {job.user_id}")
            return True
            
        except asyncio.QueueFull:
            # Remove from tracking if queue is full
            async with self._lock:
                if job.job_id in self.active_jobs:
                    del self.active_jobs[job.job_id]
                if job.user_id in self.user_active_jobs:
                    if job.job_id in self.user_active_jobs[job.user_id]:
                        self.user_active_jobs[job.user_id].remove(job.job_id)
                    if not self.user_active_jobs[job.user_id]:
                        del self.user_active_jobs[job.user_id]
            return False
    
    async def _worker(self, worker_name: str):
        """Worker task that processes jobs from the queue"""
        logger.info(f"Worker {worker_name} started")
        
        while True:
            try:
                # Get job from queue
                job = await self.processing_queue.get()
                logger.info(f"Worker {worker_name} picked up job {job.job_id} for user {job.user_id}")
                
                # Process the job
                await self._process_job(job, worker_name)
                
                # Mark task as done
                self.processing_queue.task_done()
                
            except Exception as e:
                logger.error(f"Worker {worker_name} error: {e}")
                self.processing_queue.task_done()
    
    async def _process_job(self, job: ProcessingJob, worker_name: str):
        """Process a single job"""
        session_id = None
        
        try:
            # Update status to processing
            await self._update_job_status(job, "🔄 Processing started...", worker_name)
            
            # Acquire session
            await self.session_semaphore.acquire()
            session_id = str(uuid.uuid4())
            
            logger.info(f"Worker {worker_name} acquired session {session_id[:8]} for job {job.job_id}")
            
            # Update status
            await self._update_job_status(job, "🔍 Analyzing document...", worker_name)
            
            # Process the file
            success, message, report_files = await check_file_turnitin(
                job.file_path, ACADEMI_USERNAME, ACADEMI_PASSWORD, job.user_id, session_id
            )
            
            if success and report_files:
                # Send reports to user
                await self._send_reports(job, report_files, worker_name)
                await rate_limiter.record_upload(job.user_id)
            else:
                await self._send_error(job, message, worker_name)
                
        except Exception as e:
            logger.error(f"Worker {worker_name} failed to process job {job.job_id}: {e}")
            await self._send_error(job, f"Processing error: {str(e)}", worker_name)
            
        finally:
            # Cleanup
            if session_id:
                self.session_semaphore.release()
            
            await self._cleanup_job(job)
    
    async def _update_job_status(self, job: ProcessingJob, status: str, worker_name: str):
        """Update job status message"""
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
        """Send report files to user"""
        try:
            # Update status to sending reports
            await self._update_job_status(job, f"✅ Sending {len(report_files)} reports...", worker_name)
            
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
            
            # Send completion message
            can_upload_again, files_used_now = await rate_limiter.can_upload(job.user_id)
            remaining_uploads = MAX_FILES_PER_PERIOD - files_used_now
            
            completion_text = f"""
🎉 **Analysis Complete!**

✅ Processed: `{job.filename}`
📊 Reports sent: {len(report_files)}
⚡ Worker: {worker_name}

📈 **Your Usage:**
• Files used in {RATE_LIMIT_HOURS}h: {files_used_now}/{MAX_FILES_PER_PERIOD}
• Remaining: {remaining_uploads}

Thank you! 🚀
"""
            await job.processing_message.edit_text(completion_text, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Error sending reports for job {job.job_id}: {e}")
    
    async def _send_error(self, job: ProcessingJob, error_message: str, worker_name: str):
        """Send error message to user"""
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
        """Clean up job resources"""
        try:
            # Remove from tracking
            async with self._lock:
                if job.job_id in self.active_jobs:
                    del self.active_jobs[job.job_id]
                
                if job.user_id in self.user_active_jobs:
                    if job.job_id in self.user_active_jobs[job.user_id]:
                        self.user_active_jobs[job.user_id].remove(job.job_id)
                    if not self.user_active_jobs[job.user_id]:
                        del self.user_active_jobs[job.user_id]
            
            # Cleanup temporary directory
            if os.path.exists(job.temp_dir):
                shutil.rmtree(job.temp_dir)
            
            logger.info(f"Job {job.job_id} cleaned up")
            
        except Exception as e:
            logger.error(f"Error cleaning up job {job.job_id}: {e}")
    
    async def get_queue_status(self) -> Dict[str, int]:
        """Get current queue status"""
        async with self._lock:
            total_active = len(self.active_jobs)
            per_user = {}
            for user_id, jobs in self.user_active_jobs.items():
                per_user[user_id] = len(jobs)
        
        return {
            'queue_size': self.processing_queue.qsize(),
            'total_active_jobs': total_active,
            'active_per_user': per_user
        }

class SessionManager:
    """Simplified session manager for better concurrency"""
    
    def __init__(self, max_sessions: int = MAX_CONCURRENT_SESSIONS):
        self.max_sessions = max_sessions
        self.active_sessions = {}
        self.session_semaphore = asyncio.Semaphore(max_sessions)
        self._lock = asyncio.Lock()
    
    async def get_session_id(self) -> str:
        """Get a unique session ID"""
        return str(uuid.uuid4())
    
    async def acquire_session(self, session_id: str) -> bool:
        """Acquire a session slot"""
        await self.session_semaphore.acquire()
        async with self._lock:
            self.active_sessions[session_id] = datetime.now()
        return True
    
    async def release_session(self, session_id: str):
        """Release a session slot"""
        async with self._lock:
            if session_id in self.active_sessions:
                del self.active_sessions[session_id]
        self.session_semaphore.release()
    
    async def get_active_sessions_count(self) -> int:
        """Get count of active sessions"""
        async with self._lock:
            return len(self.active_sessions)

class TurnitinChecker:
    def __init__(self, username: str, password: str, session_id: str):
        self.username = username
        self.password = password
        self.session_id = session_id
        self.session = None
        self.upload_endpoint = "https://academi.cx/dashboard/file_upload.php"
        
    async def login(self) -> bool:
        """Login to academi.cx with a fresh session"""
        logger.info(f"🔐 Logging in... (Session: {self.session_id[:8]})")
        
        # Create a new session with better configuration for concurrency
        connector = aiohttp.TCPConnector(
            limit=20,
            limit_per_host=10,
            keepalive_timeout=60,
            enable_cleanup_closed=True,
            use_dns_cache=True,
            ttl_dns_cache=300
        )
        
        timeout = aiohttp.ClientTimeout(total=90, connect=30)
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={
                'User-Agent': f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 - Session-{self.session_id[:8]}'
            }
        )
        
        login_data = {
            'email': self.username,
            'password': self.password,
            'rememberme': 'on'
        }
        
        headers = {
            'Referer': 'https://academi.cx/login',
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-Session-ID': self.session_id[:8]
        }
        
        try:
            async with self.session.post('https://academi.cx/login/login.php', 
                                       data=login_data, headers=headers) as response:
                logger.info(f"Login Status: {response.status} (Session: {self.session_id[:8]})")
                
                if response.status in [200, 302]:
                    response_text = await response.text()
                    if 'Dashboard' in response_text or 'dashboard' in response_text.lower():
                        logger.info(f"✅ Login successful! (Session: {self.session_id[:8]})")
                        return True
                    elif response.status == 302:
                        logger.info(f"✅ Login successful (redirected)! (Session: {self.session_id[:8]})")
                        return True
                
                logger.error(f"❌ Login failed (Session: {self.session_id[:8]})")
                return False
                
        except Exception as e:
            logger.error(f"❌ Login error (Session: {self.session_id[:8]}): {e}")
            return False
    
    async def upload_file(self, file_path: str) -> Optional[str]:
        """Upload file and return the file ID"""
        if not os.path.exists(file_path):
            logger.error(f"❌ File not found: {file_path} (Session: {self.session_id[:8]})")
            return None
            
        logger.info(f"📤 Uploading file: {os.path.basename(file_path)} (Session: {self.session_id[:8]})")
        
        headers = {
            'Referer': 'https://academi.cx/dashboard',
            'X-Session-ID': self.session_id[:8]
        }
        
        try:
            with open(file_path, 'rb') as f:
                data = aiohttp.FormData()
                data.add_field('file', f, filename=os.path.basename(file_path))
                
                async with self.session.post(self.upload_endpoint, 
                                           data=data, headers=headers) as response:
                    logger.info(f"Upload Status: {response.status} (Session: {self.session_id[:8]})")
                    
                    if response.status == 200:
                        # Wait for the file to be processed and get the actual file ID
                        await asyncio.sleep(3)
                        file_id = await self.get_latest_file_id()
                        if file_id:
                            logger.info(f"📋 Retrieved file ID: {file_id} (Session: {self.session_id[:8]})")
                            return file_id
                        else:
                            logger.error(f"❌ Could not retrieve file ID (Session: {self.session_id[:8]})")
                            return None
                    else:
                        response_text = await response.text()
                        logger.error(f"❌ Upload failed: {response.status}, {response_text[:300]} (Session: {self.session_id[:8]})")
        
        except Exception as e:
            logger.error(f"❌ Upload error (Session: {self.session_id[:8]}): {e}")
        
        return None
    
    async def get_latest_file_id(self) -> Optional[str]:
        """Get the latest file ID from dashboard"""
        logger.info(f"📋 Getting latest file ID from dashboard... (Session: {self.session_id[:8]})")
        
        headers = {
            'X-Session-ID': self.session_id[:8]
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
                    
                    logger.info(f"Found file IDs: {unique_file_ids} (Session: {self.session_id[:8]})")
                    
                    # Return the first (most recent) file ID
                    if unique_file_ids:
                        latest_id = unique_file_ids[0]
                        logger.info(f"Using latest file ID: {latest_id} (Session: {self.session_id[:8]})")
                        return latest_id
                    
                    # Fallback: look for any numeric IDs that might be file IDs
                    all_numbers = re.findall(r'\b\d{10,}\b', response_text)  # Look for long numbers
                    if all_numbers:
                        fallback_id = all_numbers[0]
                        logger.info(f"Using fallback file ID: {fallback_id} (Session: {self.session_id[:8]})")
                        return fallback_id
        
        except Exception as e:
            logger.error(f"❌ Error getting file ID (Session: {self.session_id[:8]}): {e}")
        
        return None
    
    async def trigger_report_generation(self, file_id: str) -> bool:
        """Trigger report generation by simulating the View Results button click"""
        logger.info(f"🔄 Triggering report generation for file ID: {file_id} (Session: {self.session_id[:8]})")
        
        headers = {
            'Referer': 'https://academi.cx/dashboard',
            'X-Requested-With': 'XMLHttpRequest',
            'X-Session-ID': self.session_id[:8]
        }
        
        # Try multiple endpoints that might trigger report generation
        trigger_urls = [
            f'https://academi.cx/dashboard/process_file.php?id={file_id}',
            f'https://academi.cx/dashboard/generate_reports.php?id={file_id}',
            f'https://academi.cx/dashboard/check_file.php?id={file_id}',
        ]
        
        for url in trigger_urls:
            try:
                async with self.session.get(url, headers=headers) as response:
                    if response.status == 200:
                        logger.info(f"✅ Trigger successful from {url} (Session: {self.session_id[:8]})")
                        return True
            except Exception as e:
                logger.debug(f"❌ Error triggering from {url} (Session: {self.session_id[:8]}): {e}")
        
        logger.info(f"ℹ️ No specific trigger found, reports should generate automatically (Session: {self.session_id[:8]})")
        return True
    
    async def wait_for_reports(self, file_id: str, max_wait_time: int = 180) -> bool:
        """Wait for AI and similarity reports to be generated"""
        logger.info(f"⏳ Waiting for reports for file ID: {file_id} (Session: {self.session_id[:8]})")
        
        start_time = time.time()
        check_interval = 20
        min_wait_time = 120
        
        # Initial wait
        logger.info(f"⏳ Initial wait of 2 minutes... (Session: {self.session_id[:8]})")
        await asyncio.sleep(min_wait_time)
        
        while time.time() - start_time < max_wait_time:
            elapsed = int(time.time() - start_time)
            minutes = elapsed // 60
            seconds = elapsed % 60
            logger.info(f"⏳ Checking reports... ({minutes}m {seconds}s elapsed) (Session: {self.session_id[:8]})")
            
            # Check if reports are ready
            ai_ready = await self.check_report_ready(file_id, 'ai_report')
            similarity_ready = await self.check_report_ready(file_id, 'similarity_report')
            
            if ai_ready and similarity_ready:
                logger.info(f"✅ Both reports ready! (Session: {self.session_id[:8]})")
                return True
            
            await asyncio.sleep(check_interval)
        
        logger.warning(f"⚠️ Timeout waiting for reports (Session: {self.session_id[:8]})")
        return True
    
    async def check_report_ready(self, file_id: str, report_type: str) -> bool:
        """Check if a specific report is ready for download"""
        url = f"https://academi.cx/dashboard/download_file.php?type={report_type}&id={file_id}"
        
        headers = {
            'Referer': 'https://academi.cx/dashboard',
            'X-Session-ID': self.session_id[:8]
        }
        
        try:
            async with self.session.head(url, headers=headers, allow_redirects=True) as response:
                if response.status == 200:
                    content_type = response.headers.get('content-type', '')
                    content_length = response.headers.get('content-length', '0')
                    
                    if ('pdf' in content_type.lower() or 
                        'application' in content_type.lower() or 
                        int(content_length) > 1000):
                        return True
                return False
        except Exception as e:
            logger.debug(f"❌ Error checking {report_type} (Session: {self.session_id[:8]}): {e}")
            return False
    
    async def download_report(self, file_id: str, report_type: str, download_dir: str) -> Optional[str]:
        """Download a specific report"""
        url = f"https://academi.cx/dashboard/download_file.php?type={report_type}&id={file_id}"
        
        headers = {
            'Referer': 'https://academi.cx/dashboard',
            'X-Session-ID': self.session_id[:8]
        }
        
        try:
            async with self.session.get(url, headers=headers) as response:
                if response.status == 200:
                    content = await response.read()
                    
                    filename = f"{report_type}_{file_id}_{self.session_id[:8]}.pdf"
                    filepath = os.path.join(download_dir, filename)
                    
                    if len(content) > 1000 and not content.startswith(b'<!DOCTYPE'):
                        with open(filepath, 'wb') as f:
                            f.write(content)
                        
                        logger.info(f"✅ Downloaded {report_type}: {len(content)} bytes (Session: {self.session_id[:8]})")
                        return filepath
                    else:
                        logger.error(f"❌ {report_type} download failed (Session: {self.session_id[:8]})")
                        return None
        
        except Exception as e:
            logger.error(f"❌ Error downloading {report_type} (Session: {self.session_id[:8]}): {e}")
            return None
    
    async def download_all_reports(self, file_id: str, download_dir: str) -> list:
        """Download all available reports for a file"""
        logger.info(f"📥 Downloading all reports for file ID: {file_id} (Session: {self.session_id[:8]})")
        
        downloaded_files = []
        report_types = ['ai_report', 'similarity_report']
        
        # Download reports concurrently
        tasks = [
            self.download_report(file_id, report_type, download_dir)
            for report_type in report_types
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for result in results:
            if isinstance(result, str) and result:
                downloaded_files.append(result)
        
        return downloaded_files
    
    async def process_file(self, file_path: str, download_dir: str = "reports") -> tuple[bool, list]:
        """Complete workflow: upload, wait, and download reports"""
        logger.info(f"🚀 Processing file: {file_path} (Session: {self.session_id[:8]})")
        
        os.makedirs(download_dir, exist_ok=True)
        
        # Upload file and get file ID
        file_id = await self.upload_file(file_path)
        if not file_id:
            return False, []
        
        # Trigger report generation
        await self.trigger_report_generation(file_id)
        
        # Wait for reports to be generated
        await self.wait_for_reports(file_id)
        
        # Download all available reports
        downloaded_files = await self.download_all_reports(file_id, download_dir)
        
        if downloaded_files:
            logger.info(f"✅ Successfully downloaded {len(downloaded_files)} report(s) (Session: {self.session_id[:8]})")
            return True, downloaded_files
        else:
            logger.error(f"❌ No reports downloaded (Session: {self.session_id[:8]})")
            return False, []

    async def close(self):
        """Close the session"""
        if self.session:
            try:
                await self.session.close()
                logger.info(f"🔒 Session closed (Session: {self.session_id[:8]})")
            except Exception as e:
                logger.error(f"Error closing session (Session: {self.session_id[:8]}): {e}")

class RateLimiter:
    def __init__(self):
        self.user_uploads: Dict[int, List[datetime]] = {}
        self.active_processing: Dict[int, int] = {}
        self._lock = asyncio.Lock()
    
    async def can_upload(self, user_id: int) -> Tuple[bool, int]:
        """Check if user can upload. Returns (can_upload, files_used_in_period)"""
        async with self._lock:
            now = datetime.now()
            cutoff_time = now - timedelta(hours=RATE_LIMIT_HOURS)
            
            if user_id not in self.user_uploads:
                self.user_uploads[user_id] = []
            
            # Remove old uploads
            self.user_uploads[user_id] = [
                upload_time for upload_time in self.user_uploads[user_id]
                if upload_time > cutoff_time
            ]
            
            files_used = len(self.user_uploads[user_id])
            active_count = self.active_processing.get(user_id, 0)
            
            can_upload = (files_used < MAX_FILES_PER_PERIOD and 
                         active_count < MAX_CONCURRENT_PER_USER)
            
            return can_upload, files_used
    
    async def record_upload(self, user_id: int):
        """Record a successful upload"""
        async with self._lock:
            if user_id not in self.user_uploads:
                self.user_uploads[user_id] = []
            
            self.user_uploads[user_id].append(datetime.now())
    
    async def start_processing(self, user_id: int):
        """Record that user started processing a file"""
        async with self._lock:
            self.active_processing[user_id] = self.active_processing.get(user_id, 0) + 1
    
    async def finish_processing(self, user_id: int):
        """Record that user finished processing a file"""
        async with self._lock:
            if user_id in self.active_processing:
                self.active_processing[user_id] = max(0, self.active_processing[user_id] - 1)
                if self.active_processing[user_id] == 0:
                    del self.active_processing[user_id]
    
    async def time_until_reset(self, user_id: int) -> timedelta:
        """Get time until user can upload again"""
        async with self._lock:
            if user_id not in self.user_uploads or not self.user_uploads[user_id]:
                return timedelta(0)
            
            oldest_upload = min(self.user_uploads[user_id])
            reset_time = oldest_upload + timedelta(hours=RATE_LIMIT_HOURS)
            now = datetime.now()
            
            if reset_time <= now:
                return timedelta(0)
            
            return reset_time - now

# Global instances
rate_limiter = RateLimiter()
session_manager = SessionManager()
processing_manager = ConcurrentProcessingManager()

# Main function to check file and return results
async def check_file_turnitin(file_path: str, username: str, password: str, user_id: int, session_id: str = None) -> tuple[bool, str, list]:
    """Main function to check file and return results"""
    if not session_id:
        session_id = await session_manager.get_session_id()
    
    checker = None
    try:
        # Acquire session slot
        await session_manager.acquire_session(session_id)
        logger.info(f"Session acquired: {session_id[:8]} for user {user_id}")
        
        checker = TurnitinChecker(username, password, session_id)
        
        if not await checker.login():
            return False, "❌ Login failed", []
        
        # Create user-specific download directory
        download_dir = f"reports/user_{user_id}_{session_id[:8]}"
        success, downloaded_files = await checker.process_file(file_path, download_dir)
        
        if success:
            return True, f"✅ File processed successfully! Downloaded {len(downloaded_files)} report(s)", downloaded_files
        else:
            return False, "❌ Failed to process file or download reports", []
    
    except Exception as e:
        logger.error(f"Error processing file for user {user_id} (Session: {session_id[:8] if session_id else 'unknown'}): {e}")
        return False, f"❌ Error: {str(e)}", []
    
    finally:
        if checker:
            try:
                await checker.close()
            except Exception as e:
                logger.error(f"Error closing checker (Session: {session_id[:8] if session_id else 'unknown'}): {e}")
        
        # Release session slot
        if session_id:
            await session_manager.release_session(session_id)
            logger.info(f"Session released: {session_id[:8]} for user {user_id}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    active_sessions = await session_manager.get_active_sessions_count()
    queue_status = await processing_manager.get_queue_status()
    
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
• {MAX_FILES_PER_PERIOD} files per {RATE_LIMIT_HOURS} hours
• Maximum file size: {MAX_FILE_SIZE // (1024*1024)}MB
• Max {MAX_CONCURRENT_PER_USER} files processing simultaneously per user
• Supported format: PDF only

📈 **System Status:**
• Active sessions: {active_sessions}/{MAX_CONCURRENT_SESSIONS}
• Queue size: {queue_status['queue_size']}
• Total active jobs: {queue_status['total_active_jobs']}

Just send me a PDF file to get started! 🚀
"""
    
    await update.message.reply_text(welcome_message, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    help_text = f"""
🔧 **Commands:**
/start - Start the bot
/help - Show this help message
/status - Check your usage status

📤 **How to check a PDF:**
1. Send me a PDF file directly in the chat
2. Your file enters the processing queue immediately
3. Multiple users can upload simultaneously
4. You'll receive detailed reports when ready

⚠️ **Important Notes:**
• Only PDF files are supported
• Maximum file size: {MAX_FILE_SIZE // (1024*1024)}MB
• Processing takes 2-3 minutes
• You can check {MAX_FILES_PER_PERIOD} files every {RATE_LIMIT_HOURS} hours
• Maximum {MAX_CONCURRENT_PER_USER} files processing at once
• **NEW**: Concurrent processing for multiple users!

🆘 **Having issues?** Make sure your file is:
• In PDF format
• Under {MAX_FILE_SIZE // (1024*1024)}MB in size
• Not password protected
"""
    
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command"""
    user_id = update.effective_user.id
    can_upload, files_used = await rate_limiter.can_upload(user_id)
    active_sessions = await session_manager.get_active_sessions_count()
    queue_status = await processing_manager.get_queue_status()
    user_active_jobs = queue_status['active_per_user'].get(user_id, 0)
    
    if can_upload:
        remaining = MAX_FILES_PER_PERIOD - files_used
        
        status_text = f"""
📊 **Your Status:**

✅ You can upload files!
📈 Files used in last {RATE_LIMIT_HOURS} hours: {files_used}/{MAX_FILES_PER_PERIOD}
🔄 Remaining uploads: {remaining}
⚡ Your active jobs: {user_active_jobs}/{MAX_CONCURRENT_PER_USER}

📈 **System Status:**
🖥️ Active sessions: {active_sessions}/{MAX_CONCURRENT_SESSIONS}
📋 Global queue size: {queue_status['queue_size']}
⚡ Total active jobs: {queue_status['total_active_jobs']}

Send me a PDF file to analyze! 📄
"""
    else:
        time_until_reset = await rate_limiter.time_until_reset(user_id)
        hours = int(time_until_reset.total_seconds() // 3600)
        minutes = int((time_until_reset.total_seconds() % 3600) // 60)
        
        if files_used >= MAX_FILES_PER_PERIOD:
            reason = f"❌ Upload limit reached ({files_used}/{MAX_FILES_PER_PERIOD})"
        elif user_active_jobs >= MAX_CONCURRENT_PER_USER:
            reason = f"⏳ Too many files processing ({user_active_jobs}/{MAX_CONCURRENT_PER_USER})"
        else:
            reason = "❌ Upload not available"
        
        status_text = f"""
📊 **Your Status:**

{reason}
📈 Files used: {files_used}/{MAX_FILES_PER_PERIOD}
⚡ Your active jobs: {user_active_jobs}/{MAX_CONCURRENT_PER_USER}
⏰ Reset in: {hours}h {minutes}m

📈 **System Status:**
🖥️ Active sessions: {active_sessions}/{MAX_CONCURRENT_SESSIONS}
📋 Global queue size: {queue_status['queue_size']}
⚡ Total active jobs: {queue_status['total_active_jobs']}

You can upload more files after the reset time or when current processing completes.
"""
    
    await update.message.reply_text(status_text, parse_mode='Markdown')

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle document uploads - Now with true concurrency!"""
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
    
    # Check rate limiting and concurrency
    can_upload, files_used = await rate_limiter.can_upload(user_id)
    if not can_upload:
        queue_status = await processing_manager.get_queue_status()
        user_active_jobs = queue_status['active_per_user'].get(user_id, 0)
        
        if user_active_jobs >= MAX_CONCURRENT_PER_USER:
            await update.message.reply_text(
                f"⏳ **Too many files processing!**\n\n"
                f"You have {user_active_jobs} files currently being processed.\n"
                f"Please wait for them to complete before uploading more.\n\n"
                f"Maximum concurrent uploads: {MAX_CONCURRENT_PER_USER}",
                parse_mode='Markdown'
            )
            return
        else:
            time_until_reset = await rate_limiter.time_until_reset(user_id)
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
    
    # Check system capacity
    queue_status = await processing_manager.get_queue_status()
    if queue_status['queue_size'] >= MAX_PROCESSING_QUEUE:
        await update.message.reply_text(
            f"🔄 **System queue full!**\n\n"
            f"All {MAX_PROCESSING_QUEUE} queue slots are currently in use.\n"
            f"Please try again in a few minutes.\n\n"
            f"Use /status to check system availability.",
            parse_mode='Markdown'
        )
        return
    
    # Create temporary directory for this processing
    temp_dir = tempfile.mkdtemp(prefix=f"user_{user_id}_")
    
    try:
        # Download the file
        file = await document.get_file()
        file_path = os.path.join(temp_dir, document.file_name)
        await file.download_to_drive(file_path)
        
        # Send immediate response that file is queued
        processing_message = await update.message.reply_text(
            f"📄 **File Queued for Processing!**\n\n"
            f"📁 File: `{document.file_name}`\n"
            f"📊 Size: {document.file_size / 1024:.1f} KB\n"
            f"🔄 Status: Added to processing queue\n"
            f"📋 Queue position: {queue_status['queue_size'] + 1}\n\n"
            f"⏱️ Processing will start shortly. Multiple files can be processed simultaneously!",
            parse_mode='Markdown'
        )
        
        # Create processing job
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
        
        # Add job to processing queue
        success = await processing_manager.add_job(job)
        
        if success:
            # Update message to show job is in queue
            await processing_message.edit_text(
                f"📄 **File Successfully Queued!**\n\n"
                f"📁 File: `{document.file_name}`\n"
                f"📊 Size: {document.file_size / 1024:.1f} KB\n"
                f"🔄 Status: In processing queue\n"
                f"🆔 Job ID: `{job.job_id[:8]}`\n\n"
                f"⏱️ Processing will begin automatically. You can send more files while this processes!",
                parse_mode='Markdown'
            )
            
            logger.info(f"✅ User {user_name} ({user_id}) queued file: {document.file_name} (Job: {job.job_id[:8]})")
            
        else:
            # Failed to add to queue (shouldn't happen due to earlier checks)
            await processing_message.edit_text(
                f"❌ **Failed to queue file**\n\n"
                f"The processing queue is full. Please try again later.",
                parse_mode='Markdown'
            )
            # Cleanup temp dir since job wasn't added
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
    
    except Exception as e:
        logger.error(f"Error handling document for user {user_id}: {e}")
        
        try:
            await update.message.reply_text(
                f"❌ **Error queuing file**\n\n"
                f"📁 File: `{document.file_name}`\n"
                f"🔄 Error: {str(e)}\n\n"
                f"Please try again later.",
                parse_mode='Markdown'
            )
        except:
            pass
        
        # Cleanup temp dir on error
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

async def handle_non_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle non-document messages"""
    queue_status = await processing_manager.get_queue_status()
    
    await update.message.reply_text(
        f"📄 **Please send a PDF file for analysis.**\n\n"
        f"I can process multiple PDFs simultaneously! Current queue: {queue_status['queue_size']} files\n\n"
        f"Send me a PDF file and I'll add it to the processing queue immediately.\n\n"
        f"Use /help for more information.",
        parse_mode='Markdown'
    )

def main():
    """Start the bot with concurrent processing"""
    print("🤖 Starting Multi-User Concurrent Telegram Bot v2.0...")
    
    # Start Flask app in a separate thread
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print(f"🌐 Flask server started on port {PORT}")
    
    # Start keep-alive pinger
    if RENDER_URL:
        print("🏓 Starting keep-alive pinger...")
        Timer(300, keep_alive).start()
    
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
    
    async def startup_callback(app):
        """Initialize processing workers on startup"""
        await processing_manager.start_workers(num_workers=8)  # Start 8 workers
        print("🔧 Processing workers started successfully")
    
    # Add startup callback
    application.post_init = startup_callback
    
    print(f"✅ Bot started successfully with TRUE concurrent processing!")
    print(f"📊 Max concurrent sessions: {MAX_CONCURRENT_SESSIONS}")
    print(f"👥 Max concurrent per user: {MAX_CONCURRENT_PER_USER}")
    print(f"📋 Max processing queue: {MAX_PROCESSING_QUEUE}")
    print(f"⚙️ Processing workers: 8")
    print("🔍 Ready to process multiple users simultaneously!")
    
    # Start polling
    application.run_polling(allowed_updates=Update.ALL_TYPES)

# Quick test function for standalone testing
async def quick_test(file_path: str, user_id: int = 12345) -> tuple[bool, list]:
    """Quick test function that returns success status and downloaded files"""
    success, message, files = await check_file_turnitin(file_path, ACADEMI_USERNAME, ACADEMI_PASSWORD, user_id)
    print(message)
    return success, files

# Test the enhanced functionality
async def test_concurrent_processing():
    """Test concurrent processing with multiple files"""
    print("🚀 Testing Concurrent Processing")
    print("=" * 50)
    
    test_file = "test.pdf"
    
    if os.path.exists(test_file):
        # Test with multiple concurrent users
        tasks = []
        for user_id in range(1, 6):  # Test with 5 users
            print(f"🔄 Starting test for user {user_id}")
            task = quick_test(test_file, user_id)
            tasks.append(task)
            # Small delay to simulate real-world timing
            await asyncio.sleep(0.5)
        
        print("⏳ Processing all files concurrently...")
        start_time = time.time()
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        end_time = time.time()
        total_time = end_time - start_time
        
        print(f"\n📊 Results (Total time: {total_time:.1f}s):")
        print("=" * 50)
        
        successful = 0
        for i, result in enumerate(results, 1):
            if isinstance(result, tuple):
                success, files = result
                status = "✅ SUCCESS" if success else "❌ FAILED"
                print(f"User {i}: {status}, Files: {len(files) if files else 0}")
                if success:
                    successful += 1
            else:
                print(f"User {i}: ❌ ERROR - {result}")
        
        print(f"\n📈 Summary: {successful}/{len(results)} successful")
        print(f"⏱️ Average time per user: {total_time/len(results):.1f}s")
        
    else:
        print(f"⚠️ Test file not found: {test_file}")

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        asyncio.run(test_concurrent_processing())
    else:
        main()