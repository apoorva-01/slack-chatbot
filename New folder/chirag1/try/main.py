from flask import Flask, request, jsonify
import os
import re
import json
from datetime import datetime
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from index_client_data import FAISSVectorStore
import asyncio
import threading
import time
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS_JSON")
SLACK_BOT_USER_ID = os.getenv("SLACK_BOT_USER_ID")

# Initialize services
faiss_store = FAISSVectorStore()
app = Flask(__name__)
genai.configure(api_key=GEMINI_API_KEY)
slack_client = WebClient(token=SLACK_BOT_TOKEN)

# Assistant mapping
ASSISTANT_SHEET_MAP = {
    "voluspa": "1WzUELoORjlIxjrb1JF2NHMIgvz73c0JIVlyjlRiAYl0",
    "mymedic": "1ker4oEDAZZFnF_SKa2JSHckmo_MoETrjJIZnQ_gtlEk",
    "manylyman": "1VkV6L_d5nNkDQ4V31RYxje9oZ-gpY8qoJS5lDDLWvqw",
    "jdsports":"1ANAHcbGYdvWISMkdIj2XSgU2cuJIR6bNNbtFBopzuX4",
    "malbongolf":"1yGm5XG-fLrxIDvYMIzcGk00vMAYO_MnKTuaPA04jLH4",
    "marijuana-packaging":"1iVPTTcVmihizLIVqoI_Mn6LdI3Zi8-GZ6hoTm9LU5q8",
    "taosfootwear":"1z3gL_3SZNm2EEsbedDzfuhxlsHTJ4A2zmSQ5slm6ovM",
    "shepherdsfashion": "1p1pshD0I008Ke0bXtPa4dHCIixukJeywm9PRvla6rKE",
    "goldenlions":"1_7bJPUMXLUhVMEjnulrWyFdALQXbm4upNPAlDCC0_Ak",
    "fullglasswineco":"1R1n4xT4BO6Zx2-c42cnHQfO1cFAv1P0S31bICkx28Mw",
    "duck-camp":"1UZ2MPVoYDQ8Jx3Qxa3nxYxPS14v67tvmxRN5cfqrxzA",
    "emazing-group":"1zDd-9U2uSFTUSeJNm5ZfUeyU2IVrlwp0BpQ4X_HC4Jw",
    "silverfernbrand":"1dUulMd2TxWAJSAL9K2AL3MJQa534TKMxuYaI7g1fXXI",
    "darntough":"1zzHnNok6AmvRJR2vVRuidIX0fVWSa093t00heltmR-U",
    "bartonwatches":"1bc7M-RbdabILwXDuWjXf0GORoYuP71xUXQlsLOIAdTU",
    "hammitt":"1Ky2StPqtvMly83SinM0nfSUa8s3Yxli3RwSH2qTAyYM",
    "dermdude":"13n-9ag7iDpkQT4D9NrJspePZwI1Y4FhFfmFkH5Dh6Lo",
    "inprintwetrustt":"1mD878GLOljX0SSDxCVC0NlYFr5kk_NnkUwmJCeixjOg",
    "florida-premium-beef":"1ZOxWDQylukRD7aLcGBorDNAflsc1eRRwYy63RxGVuII",
    "warroad":"1_2H1j05PDUofYeQ1jESUh12QNlfeNo5MIYE3FMDLLhg",
    "tailwind-nutrition":"19CrPSz1Edq8L4zfijO7_5XO64czVynj1UtD5hwPvKT4",
    "printfresh":"1Bsn082-a8-QoLGF_32JPeg4IOB7i1XbbzXQUSvPSMGQ",
    "mindbodygreen":"1HwVNSkrrLOkXnxRnT9E_FzRia3fd209itBoSW1ITYuU",
    "shes-birdie":"1B0TZStoE7mN6sMyoHQQt6jy_DKALadxOqQQ3BG9bE6o",
    "axeandsledge":"1M2pqk_0YN4OXgdkNnEqYTJAhDJjKyfM0_rS36RFMLls",
    "heybud":"1qkIcxj7sYQE_8_YYATbuNKix2qrU-CV2DQ1lpM2jT8U",
    "jones-road":"1ZK4F0CRKX8Ae0DsPBpUbiGdXbE-FNePYefw41N-ngJg",
    "createroom":"1qtqe286-p4AluC_ytAIB-K29O3-r_4zVzhoOvTXMWq8",
    "choczero":"1ZmCS7kLk7nNCJ4VVNEridof7Jb31I9uCHSBgcoPIZYY"
}
ASSISTANTS = [f.replace(".faiss", "") for f in os.listdir("faiss_index") if f.endswith(".faiss")]
print("ALL ASSISTANTS",ASSISTANTS)
specific_project_keywords = ["details of", "specific project"]
general_project_keywords = ["all projects", "list of projects"]

prompt_start=""

# Thread-safe metadata storage
thread_metadata = {}
metadata_lock = threading.Lock()


# Fetches all messages in a thread using Slack API

def get_thread_messages(channel, thread_ts):
    try:
        response = slack_client.conversations_replies(channel=channel, ts=thread_ts)
        messages = response.get("messages", [])
        formatted_output = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            text = msg.get("text", "").strip()
            if not text:
                continue

            # Skip messages that mention someone other than the bot
            mentioned_users = re.findall(r"<@([A-Z0-9]+)>", text)
            if any(user_id != SLACK_BOT_USER_ID for user_id in mentioned_users):
                continue

            is_bot = msg.get("user") == SLACK_BOT_USER_ID
            metadata = msg.get("metadata", {})

            if not is_bot:
                formatted_output.append({
                    "role": "user",
                    "text": text.replace(f"<@{SLACK_BOT_USER_ID}>", "").strip()
                })
            elif metadata.get("event_type") == "tracking_point":
                formatted_output.append({
                    "role": "assistant",
                    "text": text.strip()
                })

        return formatted_output

    except Exception as e:
        print(f"⚠️ Error fetching thread messages: {e}")
        return []


# Function to find the most relevant project name
def find_relevant_project_name(query, project_names):
    """Finds the most relevant project name using keyword matching and fuzzy matching."""
    from difflib import get_close_matches
    import re

    # Basic stop words to ignore
    stop_words = {"when", "did", "we", "deploy", "deployed", "the", "to", "of", "on", "in", "for","Hi","hello","hey","there","how","are","you","what","is","this","project","about","can","you","tell","me","more"}

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



def convert_to_slack_message(markdown_text: str) -> str:
    # Convert bold: **text** → *text*
    markdown_text = re.sub(r'\*\*(.*?)\*\*', r'*\1*', markdown_text)
    # Convert links: [text](url) → <url|text>
    markdown_text = re.sub(r'\[(.*?)\]\((.*?)\)', r'<\2|\1>', markdown_text)

    # Remove multiline code blocks: ```...```
    markdown_text = re.sub(r"```[\s\S]*?```", "", markdown_text)

    # Remove inline code blocks: `...`
    markdown_text = re.sub(r"`[^`]*`", "", markdown_text)

    return markdown_text

def preprocess_prompt_multiple_projects(prompt):
    """Expands shorthand terms and handles date-related queries before sending to Gemini."""
    month_mapping = {
        "january": "01", "jan": "01",
        "february": "02", "feb": "02",
        "march": "03", "mar": "03",
        "april": "04", "apr": "04",
        "may": "05",
        "june": "06", "jun": "06",
        "july": "07", "jul": "07",
        "august": "08", "aug": "08",
        "september": "09", "sep": "09",
        "october": "10", "oct": "10",
        "november": "11", "nov": "11",
        "december": "12", "dec": "12",
    }

    field_mapping = {
        "created": "Created Time",
        "creation": "Created Time",
        "deployed": "Deployment Date",
        "deployment": "Deployment Date",
        "due": "Original Due Date",
        "original due": "Original Due Date",
        "dev hours": "Projected Dev Hours",
        "qi hours": "Projected QI Hours",
        "total hours": "Total Project Hours",
    }

    # Normalize markdown-like symbols (e.g., *Created*) to just Created
    prompt = re.sub(r'\*(\w+)\*', r'\1', prompt)

    # Regex pattern to detect "<field> in <month> <year>"
    month_year_pattern = re.compile(
        r'\b([a-zA-Z\s]+?)\s+in\s+('
        r'jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|'
        r'january|february|march|april|may|june|july|august|september|october|november|december'
        r')\s+(\d{4})',
        re.IGNORECASE
    )

    start_date = end_date = field_name = None

    match = month_year_pattern.search(prompt)
    if match:
        field_raw, month_raw, year = match.groups()
        field_key = field_raw.strip().lower()
        month_key = month_raw.lower()
        year = int(year)

        month_num = month_mapping.get(month_key)

        # Fix: Match best key from field_mapping
        field_name = next(
            (v for k, v in field_mapping.items() if k in field_key),
            field_raw.strip()
        )

        if month_num:
            # Calculate start and end dates
            start_date_obj = datetime(year, int(month_num), 1).date()
            if month_num in ["04", "06", "09", "11"]:
                end_day = 30
            elif month_num == "02":
                end_day = 29 if year % 4 == 0 else 28
            else:
                end_day = 31
            end_date_obj = datetime(year, int(month_num), end_day).date()

            # Convert to string
            start_date = start_date_obj.isoformat()
            end_date = end_date_obj.isoformat()

            date_filter = f"{field_name} between {start_date} and {end_date}"
            prompt = month_year_pattern.sub(date_filter, prompt)

    # Expand remaining field mappings in prompt
    for short, full in field_mapping.items():
        pattern = re.compile(rf'\b{re.escape(short)}\b', flags=re.IGNORECASE)
        prompt = pattern.sub(full, prompt)

    # return prompt, start_date, end_date, field_name
    return prompt


def generate_gemini_response(user_query, thread_context, relevant_chunks=None,query_type=None, project_name=None):
    try:
        print("thread_context",thread_context)
        user_counter = 0
        assistant_counter = 0
        thread_context_lines = []

        for msg in thread_context:
            if msg["role"] == "user":
                user_counter += 1
                thread_context_lines.append(f'User Query {user_counter}: "{msg["text"]}"')
            elif msg["role"] == "assistant":
                assistant_counter += 1
                thread_context_lines.append(f'Assistant Reply {assistant_counter}: "{msg["text"]}"')
        thread_messages = " ".join(thread_context_lines)
        
        prompt = None
        if query_type=="multiple_projects":
            prompt = f"""Find all the projects details which -""" + preprocess_prompt_multiple_projects(user_query) + "\n\n" + "Conversation so far (IMPORTANT‼️: GET CONTEXT FROM HERE):" + thread_messages + "\n\n" + "IMPORTANT‼️: If the context of the project is unclear or ambiguous, do not make assumptions. Instead, ask the user to specify the project name explicitly before proceeding with the response."
        elif query_type=="specific_project":
            prompt = f"""You are a project update assistant. Find the specefic project data using following query and format into a polished, friendly message with line spaces using this style:

            "Conversation so far(IMPORTANT‼️: GET CONTEXT FROM HERE):"+{thread_messages}
            """+ user_query 
        else:
            prompt = user_query

        system_instructions = f"""
You are Assistant. You must only use the provided Relevant Information.
Do not guess or use your own knowledge. 
You are a Project Update Assistant. Follow these rules strictly when answering questions based on provided project data.

QUERY TYPE is {query_type}

===========================
🧠 FOLLOW-UP DETECTION USING `thread_context`
===========================
17. Use the `thread_context`: "{thread_context}" to determine if the current query is a follow-up.

   ➤ If `thread_context` contains one or more messages with:
     - `"role": "assistant"` followed by `"role": "user"` asking for **“details”, “more info”, “expand”, “give me the details”, or similar terms**

   ➤ If it is a follow up question - THEN treat the current query as a **follow-up** to the previous assistant response.

   ➤ If it is a follow up question - To handle this:
     - Look for the most recent answers of project results provided by the assistant.
     - If this list is available (stored as `last_matched_projects` or extractable from the previous assistant message), use it to provide specific details.
     - If not available, DO NOT guess. Prompt the user:
       > _“Could you please specify which project(s) you're referring to so I can provide accurate details?”_

18. If `thread_context` has no `"assistant"` role prior to the current user message, it is **not a follow-up**. Treat it as a fresh query.

=====================
📌 GENERAL GUIDELINES
=====================
1. ONLY use the provided project metadata, context, and relevant information. DO NOT hallucinate or use external knowledge.
2. Always prefer structured and visually clear formatting in your response.
3. If you are unsure about the project being referenced or required info is not present, ASK THE USER FOR CLARIFICATION rather than guessing.

===========================
📅 RULES FOR DATE-BASED QUERIES
===========================
4. For questions like:
   - “What projects have we completed since [month/year]?”
   - “Any deployments in [date range]?”
   - “What was completed in [Q1, March, etc.]?”

   DO THIS:
   - Check the `Deployment Date` of each project.
   - If the `Deployment Date` falls within the requested timeframe, consider it “completed.”
   - If `Deployment Date` is missing, N/A, or outside the timeframe, **DO NOT include it** in your answer.

===========================
⏱️ RULES FOR HOURS QUERIES
===========================
5. For questions involving **Development Hours**, return only the value from `Projected Dev Hours`.

6. For questions involving **Quality Inspection (QI) Hours**, return only the value from `Projected QI Hours`.

7. If asked for **Total Hours**, return the value from `Total Project Hours`.

8. NEVER sum or compute hours unless explicitly instructed by the user.

===========================
🔍 RULES FOR FIELD MATCHING
===========================
9. When a user asks about:
   - “Development”, “Dev”, “engineering time” → map to `Projected Dev Hours`
   - “QI”, “QA”, “Quality inspection” → map to `Projected QI Hours`
   - “Total time”, “Total hours” → map to `Total Project Hours`
   - “Deployment”, “Go live”, “completed” → map to `Deployment Date`

10. Always keep field matching **case-insensitive** and **context-aware**.

===========================
📄 RULES FOR MISSING DATA
===========================
11. If a field (like Deployment Date, Comments, etc.) is not available:
   - Do NOT assume or fabricate it.
   - Clearly say “Not available” or “No comments available” based on context.

===========================
 Specefic Query RULES
===========================
12. For specific_project queries, 

Find information for project {project_name}
Respond in this format:

:pushpin: **[Project Name]**  
:white_check_mark: Status: [Not started / In Progress / Completed]  
:calendar: Deployment Date: [Deployment Date or “Not available”]  
:hourglass: Total Hours: [value]  
🧑‍💻 Dev Hours: [value]  
 QI Hours: [value]  
📝 Comments: [value]  
🗓️ Created Time: [value]  
📅 Original Due Date: [value]
📅 Deployment Date: [value]
📝 Details: [value]  

🧠 If the user asks about **scope, goals, blockers, challenges, progress notes, or team decisions etc **, retrieve and summarize this information from Relevant Information:
   - 🧾 HubSpot notes
   - 📞 Meeting transcripts
   - 💬 Slack messages
   - 📝 Comments section (if relevant)

If the context includes relevant Slack/HubSpot/transcript snippets, include a **brief summary** of those in plain language (avoid quoting full blocks unless necessary). 
Example:
🔍 **Scope**: Improve checkout flow by reducing cart abandonment.  
🧠 **Challenges Discussed**: Multiple stakeholders involved in design alignment.  
📎 **Notes from Meeting [Date]**: Final dev deadline decided is [Date]

If the user’s question references something **not present in the relevant information** (e.g., scope not mentioned anywhere), respond with:
> _“Scope information was not found in the provided data. Please refer to meeting notes or provide additional context.”_

⚠️ Never invent information. Use only what’s available in structured fields or unstructured context (if provided).

===========================
 Multiple Query RULES
===========================
13. For multiple_projects QUERY FORMAT RULES:

For each matching project, format using the following structure:
  
  :pushpin: **[Project Name]**
  :white_check_mark: **Status**: Completed / In Progress / Not Started  
  :calendar: **Deployment Date**: [Deployment Date or “Not available”]  
  :hourglass: **Total Hours**: [Total Project Hours]  
  🧑‍💻 **Dev Hours**: [Projected Dev Hours]  
  🔍 **QI Hours**: [Projected QI Hours]  
  🗓️ **Created**: [Created Time or “Not available”]  
  📅 **Original Due Date**: [Original Due Date or “Not available”]  
  📅 Deployment Date: [value]
  📝 **Comments**: [Comments or “No comments available.”]
  Details: [value]  

  🔁 Leave a **blank line** between each project for better readability.

14. Add a blank line between each project section for readability.

===========================
🚫 DO NOT
===========================
15. DO NOT use today's date or current year to infer anything.
16. DO NOT include projects with missing `Deployment Date` in “completed” queries.
17. DO NOT make assumptions. Always refer strictly to the data.
"""
        model = genai.GenerativeModel(model_name="gemini-2.0-flash",
                system_instruction=system_instructions, tools=[])
        
        print("prompt",prompt,query_type,project_name)
        
        if relevant_chunks:
            prompt += (
                "\n\nIMPORTANT: Only use the information provided below to answer the question. "
                "Do not use any prior knowledge or external information.\n\n"
                "Relevant Information:\n" + "\n".join(relevant_chunks)
            )
        
        print("gemini prompt releavant chunks length",len(relevant_chunks))
        result = model.generate_content(prompt)
        print("🤖 Fetched result from Gemini with prompt length", len(prompt))
        
        if result and hasattr(result, "text"):
            response_text = result.text
            
            try:
                parsed_data = json.loads(response_text)
                if isinstance(parsed_data, list):  
                    response_text = "\n\n".join([json.dumps(item, indent=2) for item in parsed_data])
                elif isinstance(parsed_data, dict):
                    response_text = json.dumps(parsed_data, indent=2)
            except json.JSONDecodeError:
                pass
            return convert_to_slack_message(response_text)
        else:
            return "I'm sorry, but I couldn't generate a response."
    except Exception as e:
        print(f"❌ Gemini API Error: {str(e)}")
        return "❌ An error occurred while processing your request."


def gemini_response_cleaner(full_response):
    model = genai.GenerativeModel(model_name="gemini-2.0-flash",
            system_instruction="""IMPORTANT: Do not add or remove any data just format it.
            Format the following input string into a polished Slack message. 
            Use emojis, bold text where needed, and insert proper line breaks between each section for readability.""", tools=[])

    result = model.generate_content(
        """IMPORTANT: Format the following input string into a polished Slack message. 
            Follow below style format            
                       
            :pushpin: **[Project Name]**
            [Brief status update—if no work has started, mention that. If in progress, include progress notes. If completed, mention deployment date with :white_check_mark:]
            [Include any helpful links, e.g., idea brief or preview links]
            [Include estimated hours if available: dev, QI, or total hours]
            [If created time, due dates are available, mention original due date and deployment date]
            [If there are no comments, say “No comments available.”]
            Leave a blank line between each section for readability.
            [Use emojis like :pushpin:, :white_check_mark:, :hourglass_flowing_sand:, :page_facing_up:, etc. for clarity and visual appeal]\n
           """ + full_response)
    
    if result and hasattr(result, "text"):
        response_text = result.text
        try:
            parsed_data = json.loads(response_text)
            if isinstance(parsed_data, list):  
                response_text = "\n\n".join([json.dumps(item, indent=2) for item in parsed_data])
            elif isinstance(parsed_data, dict):
                response_text = json.dumps(parsed_data, indent=2)
        except json.JSONDecodeError:
            pass
            return convert_to_slack_message(response_text)
    else:
        return "I'm sorry, but I couldn't generate a response."

def store_thread_metadata(thread_ts, metadata):
    """Safely stores metadata for a thread."""
    with metadata_lock:
        thread_metadata[thread_ts] = {**thread_metadata.get(thread_ts, {}), **metadata}

def get_thread_metadata(thread_ts):
    """Safely retrieves metadata for a thread."""
    with metadata_lock:
        metadata = thread_metadata.get(thread_ts, {})
        return metadata

def extract_assistant_from_query(query, available_assistants):
    """Extracts the closest matching client name from the query."""
    for client in available_assistants:
        if re.search(rf"\b{re.escape(client)}\b", query, re.IGNORECASE):
            return client
    words_in_query = query.lower().split()
    for client in available_assistants:
        if any(word in words_in_query for word in client.lower().split()):
            return client
    return None

def get_channel_name(channel_id):
    """Fetches the channel name from the Slack API."""
    try:
        response = slack_client.conversations_info(channel=channel_id)
        if response.get("ok"):
            return response["channel"]["name"]
    except SlackApiError as e:
        print(f"❌ Slack API Error (get_channel_name): {e.response['error']}")
    return None

def fetch_google_sheet_data(sheet_id, range_name):
    """Fetches data from a Google Sheet."""
    try:
        credentials = service_account.Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDENTIALS),
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
        )
        service = build("sheets", "v4", credentials=credentials)
        result = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=range_name).execute()
        return result.get("values", [])
    except Exception as e:
        print(f"❌ Google Sheets API Error: {e}")
        return []

def send_slack_response(channel, text, thread_ts, metadata=None, attachments=None):
    """Sends a response to Slack."""
    try:
        slack_client.chat_postMessage(
            channel=channel,
            text=text,
            thread_ts=thread_ts,
            mrkdwn=True,
            metadata=metadata,
            attachments=attachments or []
        )
    except SlackApiError as e:
        print(f"❌ Slack API Error: {e.response['error']}")

def send_clarification_buttons(channel, thread_ts):
    """Sends clarification buttons to Slack."""
    if get_thread_metadata(thread_ts).get("clarification_requested"):
        print(f"🔹 Clarification already requested for thread_ts: {thread_ts}. Skipping.")
        return
    send_slack_response(
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
    return {"status": "ok"}

def handle_specific_project_query(user_query, assistant_name, channel, thread_ts):
    """Handles queries about specific projects."""
    sheet_id = ASSISTANT_SHEET_MAP[assistant_name.lower()]
    project_names = [row[0] for row in fetch_google_sheet_data(sheet_id, "Sheet1!B:B") if row]
    relevant_project = find_relevant_project_name(user_query, project_names)
    if relevant_project:
        store_thread_metadata(thread_ts, {"query": user_query, "project_name": relevant_project})
        send_slack_response(
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
        send_slack_response(channel, "I couldn't find a relevant project. Please provide more details.", thread_ts,None, [])

    return {"status": "ok"}

def send_slack_response_feedback(channel, thread_ts):
    """Send interactive 'Regenerate' button to Slack"""
    try:
        slack_client.chat_postMessage(
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
        
    except SlackApiError as e:
        print(f"❌ Slack API Error (feedback): {e.response['error']}")


def handle_project_query(user_query, assistant_name, channel, thread_ts, thread_context, query_type=None,project_name=None):
    """Handles general project-related queries."""
    if not assistant_name:
        send_slack_response(channel, ":slam: No client mentioned in query.", thread_ts,None,[])
        return
    send_slack_response(channel, f"Ok, I'm on it!  :typingcatr:", thread_ts,None,[])
    async def async_wrapper():
        await process_faiss_and_generate_responses(user_query,thread_context, assistant_name, channel, thread_ts, query_type,project_name)

    def run_async():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(async_wrapper())

    threading.Thread(target=run_async).start()

async def process_faiss_and_generate_responses(user_query, thread_context, assistant_name, channel, thread_ts,query_type,project_name):
    thread_messages = " ".join(msg["text"] for msg in thread_context)
    relevant_chunks = await async_faiss_search(user_query + thread_messages,  assistant_name) or ["No relevant data found."]
    # print("length of relevant_chunks",len(relevant_chunks))

    gemini_response = await asyncio.to_thread(generate_gemini_response, user_query,thread_context, relevant_chunks,query_type,project_name)
    final_response = await asyncio.to_thread(gemini_response_cleaner, gemini_response)
    
    send_slack_response(channel, final_response, thread_ts, 
                        {
                "event_type": "tracking_point",
                "event_payload": {
                    "status": "acknowledged",
                    "user_id": "U123456",
                    "step": "validation_passed"
                }
            },[])
    send_slack_response_feedback(channel, thread_ts)

async def async_faiss_search(full_prompt, assistant_name):
    """Performs an asynchronous FAISS search."""
    try:
        return await asyncio.to_thread(faiss_store.search_faiss, full_prompt, assistant_name)
    except Exception as e:
        print(f"❌ FAISS Error: {e}")
        return []

@app.route("/slack/events", methods=["POST"])
def slack_events():
    """Handles Slack events."""
    data = request.json

    # Handle Slack URL Verification Challenge
    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})

    # Handle Slack events
    if "event" in data:
        event = data["event"]
        event_type = event.get("type")

        # Handle app mentions
        if event_type == "app_mention":
            channel = event["channel"]
            thread_ts = event.get("thread_ts", event.get("ts"))
            user_query = event["text"].replace(f"<@{SLACK_BOT_USER_ID}>", "").strip()
            thread_context = get_thread_messages(channel, thread_ts)
            threading.Thread(target=process_gpt_query, args=(user_query, channel, thread_ts, thread_context)).start()

        # elif event_type == "message" and event.get("thread_ts"):
        #     channel = event.get("channel")
        #     thread_ts = event.get("thread_ts")
        #     user_id = event.get("user")
        #     text = event.get("text", "").strip()
        #     if user_id != SLACK_BOT_USER_ID and not re.search(r"<@U[A-Z0-9]+>", text):
        #         parent_messages = get_thread_messages(channel, thread_ts)
        #         print("parent_messages", parent_messages)
        #         if isinstance(parent_messages, str) and parent_messages:
        #             # ✅ Proceed only if bot was mentioned in the parent
        #             if f"<@{SLACK_BOT_USER_ID}>" in parent_messages:
        #                 threading.Thread(target=process_gpt_query, args=(text, channel, thread_ts, parent_messages)).start()
        #         else:
        #             print(f"⚠️ Unexpected parent_messages format or empty: {parent_messages}")
    return jsonify({"status": "ok"})

@app.route("/slack/interactive", methods=["POST"])
def slack_interactive():
    """Handles Slack interactive button clicks."""
    # try:
    data = json.loads(request.form["payload"])
    channel = data["channel"]["id"]
    thread_ts = data.get("message", {}).get("ts") or data["original_message"].get("thread_ts")
    action_value = data["actions"][0]["value"]
    metadata = get_thread_metadata(thread_ts)
    user_query = metadata.get("query")
    assistant_name = extract_assistant_from_query(get_channel_name(channel), ASSISTANTS)
    thread_context = get_thread_messages(channel, thread_ts)

    if action_value == "yes":
        store_thread_metadata(thread_ts, {"clarification_requested": "specific_project"})
        if metadata and "project_name" in metadata:
            project_name = metadata["project_name"]
            send_slack_response(channel, f":mag: Fetching details for project: *{project_name}*...", thread_ts, None, [])
            import threading
            threading.Thread(target=process_gpt_query, args=(f"Give me details of {project_name}", channel, thread_ts, thread_context, metadata)).start()
        else:
            send_slack_response(channel, ":alert: Error: Project name not found.", thread_ts,None,[])
    elif action_value == "no":
        send_slack_response(channel, "Please specify the *Project Name*", thread_ts,None,[])

    elif action_value == "regenerate":
        send_slack_response(channel, ":repeat: Regenerating response...", thread_ts,None,[])
        import threading
        threading.Thread(target=process_gpt_query, args=(user_query, channel, thread_ts, thread_context, metadata)).start()

    elif action_value == "specific_project":
        store_thread_metadata(thread_ts, {"clarification_requested": "specific_project"})
        handle_specific_project_query(user_query, assistant_name, channel, thread_ts)

    elif action_value == "multiple_projects":
        store_thread_metadata(thread_ts, {"clarification_requested": "multiple_projects"})
        handle_project_query(prompt_start + user_query, assistant_name, channel, thread_ts, thread_context, "multiple_projects")

    return jsonify({"status": "ok"})
    # except Exception as e:
    #     print(f"Error handling Slack interactive request: {str(e)}")
    #     return jsonify({"response_type": "ephemeral", "text": "An internal error occurred. Please try again later."})




def process_gpt_query(user_query, channel, thread_ts, thread_context,metadata=None):
    channel_name = get_channel_name(channel)
    # print("metadata", metadata)
    if metadata==None:
        store_thread_metadata(thread_ts, {"query": user_query})

    # Check if any other assistant is mentioned in the thread_context
    if thread_context:
        user_counter = 0
        assistant_counter = 0
        thread_context_lines = []

        for msg in thread_context:
            if msg["role"] == "user":
                user_counter += 1
                thread_context_lines.append(f'User Query {user_counter}: "{msg["text"]}"')
            elif msg["role"] == "assistant":
                assistant_counter += 1
                thread_context_lines.append(f'Assistant Reply {assistant_counter}: "{msg["text"]}"')
        thread_messages = " ".join(thread_context_lines)

        for assistant in ASSISTANTS:
            if assistant.lower() in thread_messages.lower() and assistant not in channel_name:
                send_slack_response(channel, ":slam: You are trying to access data of other clients.", thread_ts, None,[])
                return

    # Extract assistant name from the channel name
    assistant_name = extract_assistant_from_query(channel_name, ASSISTANTS) if channel_name else None
    # print(f"Assistant name extracted: {assistant_name}")
    if not assistant_name:
        send_slack_response(channel, ":slam: I do not have data for this client in my knowledge base.", thread_ts,None,[])
        return

    # Check if the user has requested clarification earlier or not
    if get_thread_metadata(thread_ts).get("clarification_requested")=="multiple_projects":
        # print("Already requested clarification-- Multiple Projects")
        handle_project_query(prompt_start+user_query, assistant_name, channel, thread_ts, thread_context,"multiple_projects")
        return
    elif get_thread_metadata(thread_ts).get("clarification_requested")=="specific_project":
        # print("Already requested clarification-- Specefic Projects")
        handle_project_query(f"Project name {get_thread_metadata(thread_ts).get("project_name")}"+user_query, assistant_name, channel, thread_ts, thread_context,"specific_project",get_thread_metadata(thread_ts).get("project_name"))
        return

    # Handle assistant-specific logic
    if any(keyword in user_query.lower() for keyword in specific_project_keywords):
        # print("No clarefication needed-- Specific Projects")
        handle_specific_project_query(user_query, assistant_name, channel, thread_ts)
        return
    elif any(keyword in user_query.lower() for keyword in general_project_keywords):
        # Multiple
        # print("No clarefication needed-- Multiple Projects")
        handle_project_query(prompt_start+user_query, assistant_name, channel, thread_ts, thread_context, "multiple_projects")
        return
    else:
        send_clarification_buttons(channel, thread_ts)
        return
    
    # print("General Query")
    handle_project_query(prompt_start+user_query, assistant_name, channel, thread_ts, thread_context,"general")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000, debug=True)