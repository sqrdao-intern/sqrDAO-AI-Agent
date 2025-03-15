import os
import logging
import traceback
import re
import json
from urllib.parse import urlparse
import requests
import trafilatura
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import google.generativeai as genai
from googleapiclient.discovery import build
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import sqlite3
from datetime import datetime
import functools
import telegram

# Load environment variables
load_dotenv()

# Configure logging with more detail
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG
)
logger = logging.getLogger(__name__)

# List of authorized member handles (without @ symbol)
AUTHORIZED_MEMBERS = [
    "member1",
    "member2",  # Add actual member handles here
]

def is_member(func):
    """Decorator to check if user is an authorized member."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user.username and user.username in AUTHORIZED_MEMBERS:
            return await func(update, context)
        else:
            await update.message.reply_text(
                "⚠️ This command is only available to sqrDAO members.\n"
                "Please contact us if you're a member and need access.",
                parse_mode=ParseMode.HTML
            )
    return wrapper

# Configure API keys
api_key = os.getenv('GEMINI_API_KEY')
google_api_key = os.getenv('GOOGLE_API_KEY')
google_cse_id = os.getenv('GOOGLE_CSE_ID')

if not api_key:
    raise ValueError("GEMINI_API_KEY not found in environment variables")
if not google_api_key:
    raise ValueError("GOOGLE_API_KEY not found in environment variables")
if not google_cse_id:
    raise ValueError("GOOGLE_CSE_ID not found in environment variables")

# Initialize Google Custom Search API
search_client = build("customsearch", "v1", developerKey=google_api_key)

# Configure Gemini
genai.configure(api_key=api_key)

# Initialize Gemini model with safety settings
try:
    generation_config = {
        "temperature": 0.9,
        "top_p": 1,
        "top_k": 1,
        "max_output_tokens": 2048,
    }

    safety_settings = [
        {
            "category": "HARM_CATEGORY_HARASSMENT",
            "threshold": "BLOCK_MEDIUM_AND_ABOVE"
        },
        {
            "category": "HARM_CATEGORY_HATE_SPEECH",
            "threshold": "BLOCK_MEDIUM_AND_ABOVE"
        },
        {
            "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
            "threshold": "BLOCK_MEDIUM_AND_ABOVE"
        },
        {
            "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
            "threshold": "BLOCK_MEDIUM_AND_ABOVE"
        },
    ]

    model = genai.GenerativeModel(
        model_name='models/gemini-1.5-pro-latest',
        generation_config=generation_config,
        safety_settings=safety_settings
    )
    
    # Test the model with a simple prompt
    test_response = model.generate_content("Hello")
    logger.info("Successfully tested model with 'Hello' prompt")
    logger.debug(f"Test response: {test_response.text if hasattr(test_response, 'text') else 'No text attribute'}")
    
except Exception as e:
    logger.error(f"Error initializing or testing Gemini model: {str(e)}")
    logger.error(f"Traceback: {traceback.format_exc()}")
    raise

def format_response_for_telegram(text):
    """Format the response text to be compatible with Telegram's HTML."""
    # First, escape special HTML characters in the text, but not in code blocks
    chunks = []
    is_code = False
    current_chunk = ""
    
    for line in text.split('\n'):
        if line.strip().startswith('```') and line.strip().endswith('```'):
            # Single-line code block
            code = line.strip()[3:-3]
            chunks.append(f"<code>{code}</code>")
            continue
        elif line.strip().startswith('```'):
            is_code = True
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = line.replace('```', '')
            continue
        elif line.strip().endswith('```') and is_code:
            is_code = False
            current_chunk += '\n' + line.replace('```', '')
            chunks.append(f"<pre>{current_chunk}</pre>")
            current_chunk = ""
            continue
            
        if is_code:
            current_chunk += '\n' + line
        else:
            # First escape HTML special characters
            line = line.replace('&', '&amp;')
            line = line.replace('<', '&lt;')
            line = line.replace('>', '&gt;')
            
            # Handle headers before other formatting
            if line.strip().startswith('## '):
                line = f"<b>{line.strip()[3:]}</b>"
            elif line.strip().startswith('# '):
                line = f"<b><u>{line.strip()[2:]}</u></b>"
            elif line.strip().startswith('### '):
                line = f"<b><i>{line.strip()[4:]}</i></b>"
            else:
                # Convert markdown to HTML, being careful with nested tags
                # Bold - replace ** with temporary markers
                line = re.sub(r'\*\*(.*?)\*\*', '[[BOLD]]\\1[[/BOLD]]', line)
                # Italic - replace * with temporary markers
                line = re.sub(r'\*(.*?)\*', '[[ITALIC]]\\1[[/ITALIC]]', line)
                # Code - replace ` with temporary markers
                line = re.sub(r'`(.*?)`', '[[CODE]]\\1[[/CODE]]', line)
                # Links - replace with temporary markers
                line = re.sub(r'\[(.*?)\]\((.*?)\)', '[[LINK]]\\1[[URL]]\\2[[/LINK]]', line)
                
                # Now replace the markers with actual HTML tags
                line = line.replace('[[BOLD]]', '<b>').replace('[[/BOLD]]', '</b>')
                line = line.replace('[[ITALIC]]', '<i>').replace('[[/ITALIC]]', '</i>')
                line = line.replace('[[CODE]]', '<code>').replace('[[/CODE]]', '</code>')
                line = re.sub(r'\[\[LINK\]\](.*?)\[\[URL\]\](.*?)\[\[/LINK\]\]', r'<a href="\2">\1</a>', line)
                
                # Handle bullet points last
                if line.strip().startswith('* '):
                    line = '• ' + line[2:]
            
            if current_chunk:
                current_chunk += '\n' + line
            else:
                current_chunk = line
    
    if current_chunk:
        chunks.append(current_chunk)
    
    result = '\n'.join(chunks)
    
    # Add debug logging
    logger.debug(f"Formatted text before sending: {result}")
    
    return result

def extract_urls(text):
    """Extract URLs from text."""
    url_pattern = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
    return re.findall(url_pattern, text)

def get_webpage_content(url):
    """Get the main content from a webpage."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            text = trafilatura.extract(downloaded)
            if text:
                return text
        
        # Fallback to basic HTML parsing if trafilatura fails
        response = requests.get(url)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Remove script and style elements
        for script in soup(["script", "style"]):
            script.decompose()
        
        text = soup.get_text()
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = ' '.join(chunk for chunk in chunks if chunk)
        
        return text[:5000]  # Limit text length
    except Exception as e:
        logger.error(f"Error fetching webpage content: {str(e)}")
        return None

def search_web(query, num_results=5):
    """Search the web using Google Custom Search API."""
    try:
        results = []
        search_results = search_client.cse().list(q=query, cx=google_cse_id, num=num_results).execute()
        
        if 'items' in search_results:
            for item in search_results['items']:
                results.append({
                    'title': item.get('title', ''),
                    'link': item.get('link', ''),
                    'snippet': item.get('snippet', '')
                })
        
        return results
    except Exception as e:
        logger.error(f"Error searching web: {str(e)}")
        return []

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /start is issued."""
    welcome_message = (
        "👋 <b>Hello!</b> I'm your AI assistant powered by Gemini. "
        "You can ask me anything, and I'll do my best to help you!\n\n"
        "I can:\n"
        "• Answer your questions about sqrDAO\n"
        "• Provide information about our platform\n"
        "• Help with general inquiries\n"
        "• Assist with platform-related questions\n\n"
        "Just send me a message or use /help to see available commands!"
    )
    await update.message.reply_text(welcome_message, parse_mode=ParseMode.HTML)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /help is issued."""
    is_authorized = update.effective_user.username in AUTHORIZED_MEMBERS
    
    help_text = """
<b>🤖 sqrDAO Bot Help</b>

I'm your AI assistant! Here's what I can do:

<b>Available Commands:</b>
• /start - Start the bot and get welcome message
• /help - Show this help message
• /about - Learn about sqrDAO
• /events - View sqrDAO events calendar
• /contact - Get contact information
"""

    if is_authorized:
        help_text += """
<b>Member-Only Commands:</b>
• /resources - Access internal resources
• /learn - Add information to knowledge base
"""

    help_text += """
<b>Features:</b>
• I remember our conversations and use them for context
• I provide detailed responses using my knowledge base
• I can help you with information about sqrDAO

Just send me a message or use any command to get started!
"""
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

class Database:
    def __init__(self):
        self.conn = sqlite3.connect('bot_memory.db')
        self.cursor = self.conn.cursor()
        self.setup_database()

    def setup_database(self):
        # Create tables for storing conversations and knowledge
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                message TEXT,
                response TEXT,
                timestamp DATETIME,
                context TEXT
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS knowledge_base (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT,
                information TEXT,
                source TEXT,
                timestamp DATETIME
            )
        ''')
        self.conn.commit()

    def store_conversation(self, user_id, message, response, context=None):
        self.cursor.execute('''
            INSERT INTO conversations (user_id, message, response, timestamp, context)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, message, response, datetime.now(), context))
        self.conn.commit()

    def store_knowledge(self, topic, information, source=None):
        self.cursor.execute('''
            INSERT INTO knowledge_base (topic, information, source, timestamp)
            VALUES (?, ?, ?, ?)
        ''', (topic, information, source, datetime.now()))
        self.conn.commit()

    def get_relevant_context(self, user_id, message, limit=5):
        # Simple keyword-based search for now
        keywords = message.lower().split()
        query = '''
            SELECT message, response, context
            FROM conversations
            WHERE user_id = ? AND (
        ''' + ' OR '.join(['lower(message) LIKE ?' for _ in keywords]) + ')'
        params = [user_id] + ['%' + keyword + '%' for keyword in keywords]
        
        self.cursor.execute(query + ' ORDER BY timestamp DESC LIMIT ?', 
                          params + [limit])
        return self.cursor.fetchall()

    def get_knowledge(self, topic):
        self.cursor.execute('''
            SELECT information
            FROM knowledge_base
            WHERE lower(topic) LIKE lower(?)
        ''', ('%' + topic + '%',))
        return self.cursor.fetchall()

# Initialize database
db = Database()

def process_message_with_context(message, context):
    # Prepare context for the model
    context_text = ""
    if context:
        context_text = "Previous relevant conversations:\n"
        for prev_msg, prev_resp, ctx in context:
            context_text += f"User: {prev_msg}\nBot: {prev_resp}\n"
    
    # Get relevant knowledge
    keywords = message.lower().split()
    knowledge_text = ""
    for keyword in keywords:
        knowledge = db.get_knowledge(keyword)
        if knowledge:
            knowledge_text += "\nStored knowledge:\n"
            for info in knowledge:
                knowledge_text += f"• {info[0]}\n"
    
    # Combine context with current message and knowledge
    prompt = f"{context_text}\n{knowledge_text}\nCurrent message: {message}\n\nPlease provide a response that takes into account both the context of previous conversations and the stored knowledge if relevant."
    
    try:
        # Generate response using Gemini
        response = model.generate_content(prompt)
        
        if not hasattr(response, 'text') or not response.text:
            return "I apologize, but I couldn't generate a response. Please try rephrasing your question."
            
        return response.text
        
    except Exception as e:
        logger.error(f"Error generating response: {str(e)}")
        return "I encountered an error while processing your message. Please try again."

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages and generate responses using Gemini."""
    user_message = update.message.text
    chat_id = update.message.chat_id
    
    if not user_message:
        await update.message.reply_text(
            "<i>I couldn't process an empty message. Please send some text.</i>",
            parse_mode=ParseMode.HTML
        )
        return
        
    logger.debug(f"Received message: {user_message}")
    
    # Check if the message is a command
    if user_message.startswith('/'):
        await update.message.reply_text(
            "<i>Please use specific commands like /help, or send a regular message.</i>",
            parse_mode=ParseMode.HTML
        )
        return
        
    try:
        # Get relevant context from previous conversations
        relevant_context = db.get_relevant_context(update.effective_user.id, user_message)
        
        # Process the message with context
        response = process_message_with_context(user_message, relevant_context)
        
        # Store the conversation
        db.store_conversation(update.effective_user.id, user_message, response)
        
        # Format and send response with HTML formatting
        formatted_text = format_response_for_telegram(response)
        logger.debug(f"Formatted text: {formatted_text}")  # Add debug logging
        
        await update.message.reply_text(
            formatted_text,
            parse_mode=ParseMode.HTML
        )
        
    except Exception as e:
        logger.error(f"Error processing message: {str(e)}")
        await update.message.reply_text(
            "<i>I encountered an error while processing your message. Please try again.</i>",
            parse_mode=ParseMode.HTML
        )

async def set_bot_commands(application):
    """Set bot commands with descriptions for the command menu."""
    # Basic commands for all users
    basic_commands = [
        ("start", "Start the bot and get welcome message"),
        ("help", "Show help and list of available commands"),
        ("about", "Learn about sqrDAO"),
        ("website", "Get sqrDAO's website"),
        ("contact", "Get contact information"),
        ("events", "View sqrDAO events")
    ]
    
    # Additional commands for members
    member_commands = basic_commands + [
        ("resources", "Access internal resources"),
        ("learn", "Add information to knowledge base")
    ]
    
    # Set basic commands for all users
    await application.bot.set_my_commands(basic_commands)
    
    # Set member commands for authorized users
    for member in AUTHORIZED_MEMBERS:
        try:
            # Get member's chat ID (this requires them to have interacted with the bot)
            # You might want to store member chat IDs in a database for better handling
            await application.bot.set_my_commands(
                member_commands,
                scope=telegram.BotCommandScopeChat(member)
            )
        except Exception as e:
            logger.error(f"Failed to set commands for member {member}: {str(e)}")

async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /about command."""
    knowledge = db.get_knowledge("sqrdao")
    about_text = "About sqrDAO:\n\n"
    if knowledge:
        about_text += knowledge[0][0]
    else:
        about_text = "sqrDAO is a decentralized autonomous organization focused on innovative blockchain solutions and research."
    await update.message.reply_text(about_text, parse_mode=ParseMode.HTML)

async def website_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /website command."""
    knowledge = db.get_knowledge("website")
    if knowledge:
        website_text = knowledge[0][0]
    else:
        website_text = "Visit sqrDAO at https://sqrdao.com"
    await update.message.reply_text(website_text, parse_mode=ParseMode.HTML)

async def contact_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /contact command."""
    contact_text = """
<b>Contact sqrDAO</b>

You can reach us through:
• Website: https://sqrdao.com
• Email: gm@sqrdao.com
• Twitter: @sqrdao
"""
    await update.message.reply_text(contact_text, parse_mode=ParseMode.HTML)

async def faq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /faq command."""
    faq_text = """
<b>Frequently Asked Questions</b>

<b>Q: What is sqrDAO?</b>
A: sqrDAO is a decentralized autonomous organization focused on innovative blockchain solutions and research.

<b>Q: How can I get started?</b>
A: Visit our website at https://sqrdao.com to learn more and join our platform.

<b>Q: What makes sqrDAO unique?</b>
A: We combine cutting-edge blockchain technology with innovative solutions to create value for our community.
"""
    await update.message.reply_text(faq_text, parse_mode=ParseMode.HTML)

async def events_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /events command."""
    events_text = """
<b>sqrDAO Events Calendar</b>

View and register for our upcoming events on Luma:
• https://lu.ma/sqrdao-events

Stay updated with our latest events, workshops, and community gatherings!
"""
    await update.message.reply_text(events_text, parse_mode=ParseMode.HTML)

@is_member
async def resources_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /resources command - Member only."""
    resources_text = """
<b>sqrDAO Internal Resources</b>

Here are our internal resources:
• <b>Notion:</b> https://sqrdao.notion.site
• <b>GitHub:</b> https://github.com/sqrdao
• <b>Drive:</b> https://drive.google.com/drive/folders/sqrdao

For access issues, please contact the team.
"""
    await update.message.reply_text(resources_text, parse_mode=ParseMode.HTML)

@is_member
async def learn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /learn command - Member only."""
    message = update.message.text.strip()
    args = message.split(None, 2)  # Split into max 3 parts
    
    if len(args) < 3:
        usage_text = """
<b>Usage:</b> /learn [topic] [information]

Example:
/learn website Our new website is live at https://sqrdao.com

This will store the information in the knowledge base for the given topic.
"""
        await update.message.reply_text(usage_text, parse_mode=ParseMode.HTML)
        return
    
    topic = args[1]
    information = args[2]
    
    try:
        db.store_knowledge(topic, information)
        await update.message.reply_text(
            f"✅ Successfully stored information about <b>{topic}</b>.",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Error storing knowledge: {str(e)}")
        await update.message.reply_text(
            "❌ Failed to store information. Please try again.",
            parse_mode=ParseMode.HTML
        )

def main():
    """Start the bot."""
    try:
        telegram_token = os.getenv('TELEGRAM_BOT_TOKEN')
        if not telegram_token:
            raise ValueError("TELEGRAM_BOT_TOKEN not found in environment variables")
            
        # Create the Application with concurrent updates
        application = Application.builder().token(telegram_token).concurrent_updates(True).build()

        # Add handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("about", about_command))
        application.add_handler(CommandHandler("website", website_command))
        application.add_handler(CommandHandler("contact", contact_command))
        application.add_handler(CommandHandler("events", events_command))
        application.add_handler(CommandHandler("resources", resources_command))
        application.add_handler(CommandHandler("learn", learn_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

        # Start the Bot
        logger.info("Starting bot...")
        application.post_init = set_bot_commands
        application.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except Exception as e:
        logger.error(f"Fatal error in main(): {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise

if __name__ == '__main__':
    main() 