import os
import re
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build
from slack_sdk.errors import SlackApiError
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
# from datetime import datetime
import threading
from dotenv import load_dotenv

from document_ids import (
    ASSISTANT_SHEET_MAP
)
load_dotenv()
SLACK_BOT_USER_ID = os.getenv("SLACK_BOT_USER_ID")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS_JSON")

# Thread-safe metadata storage
thread_metadata = {}
metadata_lock = threading.Lock()

def store_thread_metadata(thread_ts, metadata):
    """Safely stores metadata for a thread."""
    with metadata_lock:
        thread_metadata[thread_ts] = {**thread_metadata.get(thread_ts, {}), **metadata}
    return thread_metadata[thread_ts]

def get_thread_metadata(thread_ts):
    """Safely retrieves metadata for a thread."""
    with metadata_lock:
        metadata = thread_metadata.get(thread_ts, {})
        return metadata
    
# Fetches all messages in a thread using Slack API
def get_thread_messages(slack_client, channel, thread_ts):
    try:
        response = slack_client.conversations_replies(channel=channel, ts=thread_ts)
        messages = response.get("messages", [])
        formatted_output = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            text = msg.get("text", "").strip()
            
            # if not text:
            #     continue

            is_bot = msg.get("user") == SLACK_BOT_USER_ID
            metadata = msg.get("metadata", {})

            if not is_bot:
                # Skip messages that mention someone other than the bot
                mentioned_users = re.findall(r"<@([A-Z0-9]+)>", text)
                if any(user_id != SLACK_BOT_USER_ID for user_id in mentioned_users):
                    continue
                formatted_output.append({
                    "role": "user",
                    "text": text.replace(f"<@{SLACK_BOT_USER_ID}>", "").strip()
                })
            elif metadata.get("event_type") == "tracking_point":
                cleaned_text = "\n".join([line.strip() for line in text.splitlines() if line.strip()])
                formatted_output.append({
                    "role": "assistant",
                    "text": cleaned_text
                })

        return formatted_output

    except Exception as e:
        print(f"⚠️ Error fetching thread messages: {e}")
        return []
    


def find_relevant_project_name(query, project_names):
    """Finds the most relevant project name using keyword matching and fuzzy matching."""
    from difflib import get_close_matches
    import re

    # Basic stop words to ignore
    stop_words = {"when", "did", "we", "deploy", "deployed", "the", "to", "of", "on", "in", "for","hi","hello","hey","there","how","are","you","what","is","this","project","about","can","you","tell","me","more"}

    # Normalize and tokenize
    def tokenize(text):
        words = re.findall(r'\w+', text.lower())
        return set(word for word in words if word not in stop_words)

    query_keywords = tokenize(query)

    # Step 1: Keyword intersection
    best_match = None
    max_overlap = 0
    for project in project_names:
        project_keywords = tokenize(project)
        overlap = len(query_keywords & project_keywords)
        if overlap > max_overlap:
            max_overlap = overlap
            best_match = project

    if max_overlap > 0:
        return best_match

    # Step 2: Fallback to fuzzy match
    normalized_query = ''.join(tokenize(query))
    normalized_projects = {
        name: ''.join(tokenize(name))
        for name in project_names
    }

    matches = get_close_matches(normalized_query, normalized_projects.values(), n=1, cutoff=0.5)
    if matches:
        for original_name, normalized_name in normalized_projects.items():
            if matches[0] == normalized_name:
                return original_name
    return None

def fetch_google_sheet_data(sheet_id, range_name, google_credentials):
    """Fetches data from a Google Sheet."""
    try:
        credentials = service_account.Credentials.from_service_account_info(
            json.loads(google_credentials),
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
        )
        service = build("sheets", "v4", credentials=credentials)
        result = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=range_name).execute()
        return result.get("values", [])
    except Exception as e:
        print(f"❌ Google Sheets API Error: {e}")
        return []

def get_channel_name(channel_id, slack_client):
    """Fetches the channel name from the Slack API."""
    try:
        response = slack_client.conversations_info(channel=channel_id)
        if response.get("ok"):
            return response["channel"]["name"]
    except SlackApiError as e:
        print(f"❌ Slack API Error (get_channel_name): {e.response['error']}")
    return None

def extract_assistant_from_channel_name(channel_name, available_assistants):
    """Extracts the base client name (e.g., 'voluspa') from the channel name."""
    normalized_channel = channel_name.lower().replace('-', '_')
    channel_words = set(normalized_channel.split('_'))
    # Extract base client names from assistant list (e.g., 'voluspa' from 'voluspa_notion')
    client_basenames = {a.split('_')[0].lower() for a in available_assistants}

    for word in channel_words:
        if word in client_basenames:
            return word
    # Fallback: partial match
    # for base_name in client_basenames:
    #     if base_name in normalized_channel:
    #         return base_name

    return None

def send_slack_response(slack_client, channel, text, thread_ts, metadata=None, attachments=None):
    """Sends a response to Slack."""
    try:
        response = slack_client.chat_postMessage(
            channel=channel,
            text=text,
            thread_ts=thread_ts,
            mrkdwn=True,
            metadata=metadata,
            attachments=attachments or []
        )
        return response 
    except SlackApiError as e:
        print(f"❌ Slack API Error: {e.response['error']}")
        return None

def send_clarification_buttons(slack_client, channel, thread_ts):
    """Sends clarification buttons to Slack."""
    try:
        if get_thread_metadata(thread_ts).get("clarification_requested"):
            print(f"🔹 Clarification already requested for thread_ts: {thread_ts}. Skipping.")
            return
        response = send_slack_response(
            slack_client,
            channel,
            "Could you clarify if you're asking about a specific project or multiple projects?",
            thread_ts,
            attachments=[
                {
                    "fallback": "Unable to clarify query",
                    "callback_id": "clarification_request",
                    "actions": [
                        {"name": "clarify", "text": "Specific Project", "type": "button", "value": "specific_project"},
                        {"name": "clarify", "text": "Multiple Projects", "type": "button", "value": "multiple_projects"},
                    ],
                }
            ]
        )
        return response
    except SlackApiError as e:
        print(f"❌ Slack API Error: {e.response['error']}")
        return None
    

def send_slack_response_feedback(slack_client, channel, thread_ts):
    """Send interactive 'Regenerate' button to Slack"""
    try:
        response = slack_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            attachments=[
                {
                    "text": "Did this answer your question?",
                    "fallback": "Unable to give feedback",
                    "callback_id": "feedback_response",
                    "actions": [
                        {"name": "feedback", "text": "🔄 Regenerate", "type": "button", "value": "regenerate"},
                    ],
                }
            ],
        )
        return response
    except SlackApiError as e:
        print(f"❌ Slack API Error: {e.response['error']}")
        return None
    
def send_specific_project_confirmation_button(slack_client, user_query, assistant_name, channel, thread_ts):
    """Handles queries about specific projects."""
    try:
        sheet_id = ASSISTANT_SHEET_MAP[assistant_name.lower()]
        project_names = [row[0] for row in fetch_google_sheet_data(sheet_id, "Sheet1!B:B",GOOGLE_CREDENTIALS) if row]
        relevant_project = find_relevant_project_name(user_query, project_names)
        if relevant_project:
            store_thread_metadata(thread_ts, {"query": user_query, "project_name": relevant_project})
            send_slack_response(
                slack_client,
                channel,
                f"Did you mean the project: *{relevant_project}*?",
                thread_ts,
                attachments=[
                    {
                        "fallback": "Unable to confirm project",
                        "callback_id": "project_confirmation",
                        "actions": [
                            {"name": "confirm", "text": "✅ Yes", "type": "button", "value": "yes"},
                            {"name": "confirm", "text": "❌ No", "type": "button", "value": "no"},
                        ],
                    }
                ]
            )
        else:
            send_slack_response(slack_client,channel, "I couldn't find a relevant project. Please provide more details.", thread_ts,None, [])

    except SlackApiError as e:
        print(f"❌ Slack API Error: {e.response['error']}")
        return None



def convert_to_slack_message(markdown_text: str) -> str:
    # Convert <br> to newline
    markdown_text = markdown_text.replace('<br>', '\n')
    # Convert custom bold: !!text!! → *text*
    markdown_text = re.sub(r'!!(.*?)!!', r'*\1*', markdown_text)
    
    # Convert italic: __text__ → _text_
    markdown_text = re.sub(r'__(.*?)__', r'_\1_', markdown_text)
    
    # Convert links: [text](url) → <url|text>
    markdown_text = re.sub(r'\[(.*?)\]\((.*?)\)', r'<\2|\1>', markdown_text)
    
    # Convert dash bullets (- ) to numbered list: 1., 2., ...
    lines = markdown_text.splitlines()
    numbered_lines = []
    counter = 1
    for line in lines:
        if re.match(r'^\s*-\s+', line):
            content = re.sub(r'^\s*-\s+', '', line)
            numbered_lines.append(f"{counter}. {content}")
            counter += 1
        else:
            numbered_lines.append(line)
    markdown_text = '\n'.join(numbered_lines)
    
    # Convert :: prefix to unstyled bullet → •
    markdown_text = re.sub(r'^\s*::\s*(.*)', r'• \1', markdown_text, flags=re.MULTILINE)
    
    # Convert >> prefix to block quote → >
    markdown_text = re.sub(r'^\s*>>\s*(.*)', r'> \1', markdown_text, flags=re.MULTILINE)
    return markdown_text

def strip_json_wrapper(text):
    # Remove code block wrappers like ```json or ```text
    text = re.sub(r'^\s*```(?:json|text)?\s*', '', text.strip(), flags=re.IGNORECASE)
    text = re.sub(r'\s*```$', '', text.strip())
    # Remove Markdown bold (**text**)
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    # If it's a single-field JSON string, extract the value (safely)
    match = re.search(r':\s*"([^"]+)"', text)
    if match:
        return match.group(1)
    # Remove leading 'json' or 'text' identifiers
    text = re.sub(r"^(json|text)\s*", "", text.strip(), flags=re.IGNORECASE)
    # Remove tool_code key/label if it's showing up
    text = re.sub(r"(?i)^tool_code\s*[:=]\s*", "", text.strip())

    return text.strip()




def preprocess_relative_dates(query):
    today = datetime.today()

    def format_date(date_obj):
        return date_obj.strftime('%Y-%m-%d')

    def month_range(date_obj):
        start = date_obj.replace(day=1)
        next_month = start + relativedelta(months=1)
        end = next_month - timedelta(days=1)
        return format_date(start), format_date(end)

    def week_range(date_obj):
        start = date_obj - timedelta(days=date_obj.weekday())
        end = start + timedelta(days=6)
        return format_date(start), format_date(end)

    def year_range(date_obj):
        start = date_obj.replace(month=1, day=1)
        end = date_obj.replace(month=12, day=31)
        return format_date(start), format_date(end)

    def quarter_range(date_obj):
        month = ((date_obj.month - 1) // 3) * 3 + 1
        start = date_obj.replace(month=month, day=1)
        end = (start + relativedelta(months=3)) - timedelta(days=1)
        return format_date(start), format_date(end)

    def shift_quarter(date_obj, shift):
        month = ((date_obj.month - 1) // 3) * 3 + 1
        current_q_start = date_obj.replace(month=month, day=1)
        target_q_start = current_q_start + relativedelta(months=3*shift)
        return quarter_range(target_q_start)

    replacements = {
        r"\btoday\b": format_date(today),
        r"\byesterday\b": format_date(today - timedelta(days=1)),
        r"\btomorrow\b": format_date(today + timedelta(days=1)),

        # Weeks
        r"\bthis week\b": f'in: {week_range(today)}',
        r"\bnext week\b": f'in: {week_range(today + timedelta(days=7))}',
        r"\blast week\b": f'in: {week_range(today - timedelta(days=7))}',
        r"\bcoming week\b": f'in: {week_range(today + timedelta(days=7))}',
        r"\bprevious week\b": f'in: {week_range(today - timedelta(days=7))}',

        # Months
        r"\bthis month\b": f'in: {month_range(today)}',
        r"\bnext month\b": f'in: {month_range(today + relativedelta(months=1))}',
        r"\blast month\b": f'in: {month_range(today - relativedelta(months=1))}',
        r"\bcoming month\b": f'in: {month_range(today + relativedelta(months=1))}',
        r"\bprevious month\b": f'in: {month_range(today - relativedelta(months=1))}',

        # Years
        r"\bthis year\b": f'in: {year_range(today)}',
        r"\bthis yr\b": f'in: {year_range(today)}',
        r"\bnext year\b": f'in: {year_range(today + relativedelta(years=1))}',
        r"\bnext yr\b": f'in: {year_range(today + relativedelta(years=1))}',
        r"\blast year\b": f'in: {year_range(today - relativedelta(years=1))}',
        r"\blast yr\b": f'in: {year_range(today - relativedelta(years=1))}',

        # Quarters
        r"\bthis quarter\b": f'in: {quarter_range(today)}',
        r"\blast quarter\b": f'in: {shift_quarter(today, -1)}',
        r"\bnext quarter\b": f'in: {shift_quarter(today, 1)}',
    }

    dynamic_patterns = [
        (r"\b(last|past)\s+(\d+)\s+days\b", lambda m: f'in: {format_date(today - timedelta(days=int(m.group(2))))}, {format_date(today)}'),
        (r"\bnext\s+(\d+)\s+days\b", lambda m: f'in: {format_date(today)}, {format_date(today + timedelta(days=int(m.group(1))))}'),

        (r"\b(last|past)\s+(\d+)\s+weeks\b", lambda m: f'in: {format_date(today - timedelta(weeks=int(m.group(2))))}, {format_date(today)}'),
        (r"\bnext\s+(\d+)\s+weeks\b", lambda m: f'in: {format_date(today)}, {format_date(today + timedelta(weeks=int(m.group(1))))}'),

        (r"\b(last|past)\s+(\d+)\s+months\b", lambda m: f'in: {format_date(today - relativedelta(months=int(m.group(2))))}, {format_date(today)}'),
        (r"\bnext\s+(\d+)\s+months\b", lambda m: f'in: {format_date(today)}, {format_date(today + relativedelta(months=int(m.group(1))))}'),

        (r"\b(last|past)\s+(\d+)\s+years\b", lambda m: f'in: {format_date(today - relativedelta(years=int(m.group(2))))}, {format_date(today)}'),
        (r"\bnext\s+(\d+)\s+years\b", lambda m: f'in: {format_date(today)}, {format_date(today + relativedelta(years=int(m.group(1))))}'),
    ]

    special_ranges = {
        r"\bstart of this month\b": month_range(today)[0],
        r"\bend of this month\b": month_range(today)[1],
        r"\bstart of last month\b": month_range(today - relativedelta(months=1))[0],
        r"\bend of last month\b": month_range(today - relativedelta(months=1))[1],

        r"\bstart of this week\b": week_range(today)[0],
        r"\bend of this week\b": week_range(today)[1],
        r"\bstart of last week\b": week_range(today - timedelta(days=7))[0],
        r"\bend of last week\b": week_range(today - timedelta(days=7))[1],

        r"\bstart of this year\b": year_range(today)[0],
        r"\bend of this year\b": year_range(today)[1],

        r"\bstart of this quarter\b": quarter_range(today)[0],
        r"\bend of this quarter\b": quarter_range(today)[1],
        r"\bstart of last quarter\b": shift_quarter(today, -1)[0],
        r"\bend of last quarter\b": shift_quarter(today, -1)[1],
    }

    for pattern, replacement in replacements.items():
        query = re.sub(pattern, replacement, query, flags=re.IGNORECASE)

    for pattern, func in dynamic_patterns:
        query = re.sub(pattern, func, query, flags=re.IGNORECASE)

    for pattern, date_str in special_ranges.items():
        query = re.sub(pattern, date_str, query, flags=re.IGNORECASE)

    return query

# def preprocess_prompt_multiple_projects(prompt):
#     """Expands shorthand terms and handles date-related queries before sending to Gemini."""
#     month_mapping = {
#         "january": "01", "jan": "01",
#         "february": "02", "feb": "02",
#         "march": "03", "mar": "03",
#         "april": "04", "apr": "04",
#         "may": "05",
#         "june": "06", "jun": "06",
#         "july": "07", "jul": "07",
#         "august": "08", "aug": "08",
#         "september": "09", "sep": "09",
#         "october": "10", "oct": "10",
#         "november": "11", "nov": "11",
#         "december": "12", "dec": "12",
#     }

#     field_mapping = {
#         "created": "Created Time",
#         "creation": "Created Time",
#         "deployed": "Deployment Date",
#         "deployment": "Deployment Date",
#         "due": "Original Due Date",
#         "original due": "Original Due Date",
#         "dev hours": "Projected Dev Hours",
#         "qi hours": "Projected QI Hours",
#         "total hours": "Total Project Hours",
#     }

#     # Normalize markdown-like symbols (e.g., *Created*) to just Created
#     prompt = re.sub(r'\*(\w+)\*', r'\1', prompt)

#     # Regex pattern to detect "<field> in <month> <year>"
#     month_year_pattern = re.compile(
#         r'\b([a-zA-Z\s]+?)\s+in\s+('
#         r'jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|'
#         r'january|february|march|april|may|june|july|august|september|october|november|december'
#         r')\s+(\d{4})',
#         re.IGNORECASE
#     )

#     start_date = end_date = field_name = None

#     match = month_year_pattern.search(prompt)
#     if match:
#         field_raw, month_raw, year = match.groups()
#         field_key = field_raw.strip().lower()
#         month_key = month_raw.lower()
#         year = int(year)

#         month_num = month_mapping.get(month_key)

#         # Fix: Match best key from field_mapping
#         field_name = next(
#             (v for k, v in field_mapping.items() if k in field_key),
#             field_raw.strip()
#         )

#         if month_num:
#             # Calculate start and end dates
#             start_date_obj = datetime(year, int(month_num), 1).date()
#             if month_num in ["04", "06", "09", "11"]:
#                 end_day = 30
#             elif month_num == "02":
#                 end_day = 29 if year % 4 == 0 else 28
#             else:
#                 end_day = 31
#             end_date_obj = datetime(year, int(month_num), end_day).date()

#             # Convert to string
#             start_date = start_date_obj.isoformat()
#             end_date = end_date_obj.isoformat()

#             date_filter = f"{field_name} between {start_date} and {end_date}"
#             prompt = month_year_pattern.sub(date_filter, prompt)

#     # Expand remaining field mappings in prompt
#     for short, full in field_mapping.items():
#         pattern = re.compile(rf'\b{re.escape(short)}\b', flags=re.IGNORECASE)
#         prompt = pattern.sub(full, prompt)

#     return prompt