from flask import Flask, request, jsonify, make_response
import os
import json
import google.generativeai as genai
from dotenv import load_dotenv
from slack_sdk import WebClient
from index_client_data import FAISSVectorStore
from utils import (
    classify_multiple_projects_query_intent,
    get_thread_metadata,
    store_thread_metadata,
    convert_to_slack_message,
    extract_assistant_from_channel_name,
    get_channel_name,
    send_slack_response,
    send_clarification_buttons,
    send_slack_response_feedback,
    get_thread_messages,
    send_specific_project_confirmation_button,
    strip_json_wrapper
)
from filter_logic import (
    generate_gemini_parsed_query,
    convert_parsed_query_to_filter,
    query_notion_projects,
    format_multiple_projects_flash_message
)
from system_instruction import get_system_instructions
import asyncio
import threading
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

ASSISTANTS = [f.replace(".faiss", "") for f in os.listdir("faiss_index") if f.endswith(".faiss")]



def get_multiple_projects_from_thread_context(thread_context):
    try:
        system_instructions = f"""
You are an expert assistant that extracts all project names from a conversation thread.

Input:
- You will receive a list of messages. Each message is a dictionary with two fields:
  - 'role': either 'user' or 'assistant'
  - 'text': the actual message content

Goal:
- Identify and return all project names mentioned in any of the messages (from both user and assistant).
- A project name is typically in formats like: "SF - Inconsistencies 7", "NYC - Issues 5", etc.
- Extract only the unique project names. Preserve their original wording and order of appearance.

Output:
- Return a JSON array (list) of the extracted project names as plain strings.
- Example:
  ["SF - Inconsistencies 7", "SF - Inconsistencies 5"]
"""
        model = genai.GenerativeModel(model_name="gemini-2.0-flash", system_instruction=system_instructions, tools=[])
        prompt = f"Here is the conversation:\n{thread_context}\n\nReturn a JSON array of all project names mentioned."
        result = model.generate_content(prompt)

        if result and hasattr(result, "text"):
            try:
                cleaned_response = result.text.strip()
                if cleaned_response.startswith("```json"):
                    cleaned_response = cleaned_response[7:].strip()  # Remove ```json prefix
                if cleaned_response.endswith("```"):
                    cleaned_response = cleaned_response[:-3].strip()  # Remove ``` suffix

                project_names = json.loads(cleaned_response)
                return project_names  # Return as a Python list
            except json.JSONDecodeError as e:
                print(f"❌ Failed to decode JSON response: {e}")
                return []
        else:
            return []
    except Exception as e:
        print(f"❌ Gemini API Error: {str(e)}")
        return []



def generate_gemini_response(query_type, processed_query, thread_messages, notion_chunks, hubspot_chunks, raw_messages_chunks, transcript_chunks, faq_chunks, internal_slack_messages_chunks, project_name=None, multiple_projects_array=None):
    if(multiple_projects_array):
        print("multiple_projects_array",multiple_projects_array)
    system_instructions = get_system_instructions(query_type, False, processed_query, project_name, multiple_projects_array)
    model = genai.GenerativeModel(model_name="gemini-2.0-flash", system_instruction=system_instructions, tools=[])
    prompt = f"""
    QUERY_TYPE = `{query_type}`
            
    USER_QUERY = `{processed_query}`

    Conversation so far (IMPORTANT‼️: GET CONTEXT FROM HERE):
    `{thread_messages}`

    Projects data from Notion:
    `{notion_chunks if notion_chunks else "No Notion data provided."}`

    Emails and Communication data from HubSpot:
    `{hubspot_chunks if hubspot_chunks else "No Emails/HubSpot data provided."}`

    Client/Partner Slack Messages:
    `{raw_messages_chunks if raw_messages_chunks else "No Client/Partner Slack Messages available."}`
                    
    Meeting transcript highlights:
    `{transcript_chunks if transcript_chunks else "No transcript info available."}`

    Internal Slack messages:
    `{internal_slack_messages_chunks if internal_slack_messages_chunks else "No Internal Slack messages available."}`

    Question & Answers:
    `{faq_chunks if faq_chunks else "No Internal Slack messages available."}`
    """
    result = model.generate_content(prompt)
    if result and hasattr(result, "text"):
        response_text = strip_json_wrapper(result.text)
        return convert_to_slack_message(response_text)
    else:
        return "I'm sorry, but I couldn't generate a response."

def generate_custom_filter_response(user_query, notion_chunks):
    parsed_query = generate_gemini_parsed_query(user_query)
    notion_filter = convert_parsed_query_to_filter(parsed_query)
    print("notion_filter", notion_filter)
    matching_projects = query_notion_projects(notion_filter, notion_chunks)
    print("matching_projects", len(matching_projects))
    if not matching_projects:
        return "No projects found matching that criteria 😕"
    return convert_to_slack_message(format_multiple_projects_flash_message(matching_projects, parsed_query))
                    

def generate_final_response(user_query, is_follow_up, thread_context, thread_messages, notion_chunks=None, hubspot_chunks=None, raw_messages_chunks=None, transcript_chunks=None, faq_chunks=None, internal_slack_messages_chunks=None, query_type=None, user_slack_id=None, project_name=None, multiple_projects_array=None):
    try:
        result = None
        print("is_follow_up", is_follow_up)
        if query_type == "multiple_projects":
            if not is_follow_up:
                # First message in thread → always try filtering
                result = generate_custom_filter_response(user_query, notion_chunks)
            else:
                # Follow-up → classify query intent
                if classify_multiple_projects_query_intent(user_query):
                    print("🔎 Classified as filtering query")
                    result = generate_custom_filter_response(user_query, notion_chunks)
                else:
                    print("💬 Classified as non-filtering query")
                    result = generate_gemini_response(
                        query_type, user_query, thread_messages, notion_chunks, hubspot_chunks,
                        raw_messages_chunks, transcript_chunks, faq_chunks,
                        internal_slack_messages_chunks, project_name, multiple_projects_array
                    )
        else:
            processed_query = user_query
            result = generate_gemini_response(
                query_type, processed_query, thread_messages, notion_chunks, hubspot_chunks, raw_messages_chunks,
                transcript_chunks, faq_chunks, internal_slack_messages_chunks, project_name, multiple_projects_array
            )
        return result
    except Exception as e:
        print(f"❌ Gemini API Error: {str(e)}")


def initiate_gpt_query(user_query, assistant_name, channel, thread_ts, thread_context, query_type=None, user_slack_id=None,project_name=None):
    if not assistant_name:
        send_slack_response(slack_client, channel, ":slam: I do not have data for this client in my knowledge base.", thread_ts,None,[])
        return
    typing_message = send_slack_response(slack_client, channel, f"Ok, I'm on it!  :typingcatr:", thread_ts,None,[])
    typing_ts = typing_message.get("ts") if typing_message else None
    async def async_wrapper():
        await process_faiss_and_generate_responses(user_query, thread_context, assistant_name, channel, thread_ts, query_type, user_slack_id, project_name)
        if typing_ts:
            try:
                slack_client.chat_delete(channel=channel, ts=typing_ts)
            except Exception as e:
                print("❗️ Failed to delete typing message:", e)

    def run_async():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(async_wrapper())
        finally:
            loop.close()

    threading.Thread(target=run_async).start()

async def process_faiss_and_generate_responses(user_query, thread_context, assistant_name, channel, thread_ts, query_type, user_slack_id, project_name):
    is_follow_up = False
    multiple_projects_array = None
    combined_string =""
    user_counter = 0
    assistant_counter = 0
    thread_context_lines = []
    for msg in thread_context:
        if msg["role"] == "user":
            user_counter += 1
            thread_context_lines.append(f'User Query {user_counter}: "{msg["text"]}"\n')
        elif msg["role"] == "assistant":
            is_follow_up = True
            assistant_counter += 1
            thread_context_lines.append(f'Assistant Reply {assistant_counter}: "{msg["text"]}"\n')
    thread_messages = " ".join(thread_context_lines)
    if query_type == "multiple_projects":
        if(is_follow_up):
            multiple_projects_array = get_multiple_projects_from_thread_context(thread_context)
            if isinstance(multiple_projects_array, list) and all(isinstance(i, str) for i in multiple_projects_array):
                combined_string = " ".join(multiple_projects_array)
    query_to_search = f"{project_name} {user_query}" if project_name else f"{combined_string} {user_query}" 
    faiss_result = await async_faiss_search(query_to_search, assistant_name,channel,thread_ts)
    notion_chunks = faiss_result.get("notion_chunks", ["No relevant data found."])
    hubspot_chunks = faiss_result.get("hubspot_chunks", ["No relevant data found."])
    raw_messages_chunks = faiss_result.get("raw_messages_chunks", ["No relevant data found."])
    transcript_chunks = faiss_result.get("transcript_chunks", ["No relevant data found."])
    faq_chunks = faiss_result.get("faq_chunks", ["No relevant data found."])
    internal_slack_messages_chunks = faiss_result.get("internal_slack_messages_chunks", ["No relevant data found."])
    # print("notion_chunks",len(notion_chunks),"hubspot_chunks",len(hubspot_chunks),"raw_messages_chunks",len(raw_messages_chunks),"transcript_chunks",len(transcript_chunks),"faq_chunks",len(faq_chunks),"internal_slack_messages_chunks",len(internal_slack_messages_chunks))
    
    gemini_response = await asyncio.to_thread(generate_final_response, user_query, is_follow_up, thread_context, thread_messages, notion_chunks,hubspot_chunks,raw_messages_chunks, transcript_chunks,faq_chunks, internal_slack_messages_chunks, query_type, user_slack_id, project_name, multiple_projects_array)
    send_slack_response(slack_client,channel, gemini_response, thread_ts, {
                "event_type": "tracking_point",
                "event_payload": {
                    "status": "acknowledged",
                    "user_id": "U123456",
                    "step": "validation_passed"
                }
            },[])
    
    send_slack_response_feedback(slack_client, channel, thread_ts)

async def async_faiss_search(full_prompt, assistant_name,channel,thread_ts):
    """Performs an asynchronous FAISS search."""
    try:
        return await asyncio.to_thread(faiss_store.search_faiss, full_prompt, assistant_name)
    except Exception as e:
        print(f"❌ FAISS Error: {e}")
        send_slack_response(slack_client,channel,"Hey <@U08B0GKSTGF>, I’m broken 🫠 Got a query indexing error... fix me fast, I have work to do :typingcat:", thread_ts, None, [])
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
            user_slack_id =  event["user"]
            user_query = event["text"].replace(f"<@{SLACK_BOT_USER_ID}>", "").strip()
            thread_context = get_thread_messages(slack_client, channel, thread_ts)
            threading.Thread(target=handle_slack_actions, args=(user_query, channel, thread_ts, thread_context, user_slack_id)).start()

    return make_response(jsonify({"status": "ok"}), 200)

@app.route("/slack/interactive", methods=["POST"])
def slack_interactive():
    """Handles Slack interactive button clicks."""
    try:
        data = json.loads(request.form["payload"])
        channel_id = data["channel"]["id"]
        thread_ts = data.get("message", {}).get("ts") or data["original_message"].get("thread_ts")
        action_value = data["actions"][0]["value"]
        metadata = get_thread_metadata(thread_ts)
        user_slack_id = data["user"]["id"]
        user_query = metadata.get("query")
        assistant_name = extract_assistant_from_channel_name(get_channel_name(channel_id,slack_client), ASSISTANTS)
        thread_context = get_thread_messages(slack_client, channel_id, thread_ts)
        message_data = data.get("message") or data.get("original_message") or {}
        message_ts = message_data.get("ts")

        if action_value == "yes":
            store_thread_metadata(thread_ts, {"clarification_requested": "specific_project"})
            if metadata and "project_name" in metadata:
                import threading
                project_name = metadata["project_name"]
                threading.Thread(target=handle_slack_actions, args=(user_query, channel_id, thread_ts, thread_context, user_slack_id, metadata,message_ts,project_name)).start()
       
            else:
                send_slack_response(slack_client, channel_id, "Context is expired, could you please ask your query again", thread_ts, None, [])

        elif action_value == "no":
            send_slack_response(slack_client, channel_id, "Please specify the *Project Name*", thread_ts, None, [])
            slack_client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    text=f"*Please Try Again*",
                    attachments=[]
                )
        
        elif action_value == "regenerate":
            import threading
            threading.Thread(target=handle_slack_actions, args=(user_query, channel_id, thread_ts, thread_context, user_slack_id, metadata, message_ts,":repeat: Regenerating response...")).start()
        
        elif action_value == "specific_project":
            store_thread_metadata(thread_ts, {"clarification_requested": "specific_project"})
            send_specific_project_confirmation_button(slack_client, user_query, assistant_name, channel_id, thread_ts)
            slack_client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text="*Specific Project*",
                attachments=[]
            )
        
        elif action_value == "multiple_projects":
            store_thread_metadata(thread_ts, {"clarification_requested": "multiple_projects"})
            initiate_gpt_query(user_query, assistant_name, channel_id, thread_ts, thread_context, "multiple_projects", user_slack_id, None)
            slack_client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text="*Multiple Projects*",
                attachments=[]
            )
        return make_response(b"", 200)
    except Exception as e:
        print(f"Error handling Slack interactive request: {str(e)}")
        return make_response(jsonify({"status": "Error", "message": str(e)}).get_data(), 500)


def handle_slack_actions(user_query, channel_id, thread_ts, thread_context,user_slack_id,metadata=None,message_ts=None,message_text=None):
    channel_name = get_channel_name(channel_id,slack_client)
    if metadata == None:
        store_thread_metadata(thread_ts, {"query": user_query})

    # Check if any other assistant is mentioned in the thread_context or not
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
                send_slack_response(slack_client, channel_id, F":slam: Hey <@{user_slack_id}>, you are trying to access data of other clients.", thread_ts, None,[])
                return

    # Extract assistant name from the channel name
    assistant_name = extract_assistant_from_channel_name(channel_name, ASSISTANTS) if channel_name else None
    if not assistant_name:
        send_slack_response(slack_client, channel_id, ":slam: I do not have data for this client in my knowledge base.", thread_ts,None,[])
        return
               
    # Check if the user has requested clarification earlier or not
    if get_thread_metadata(thread_ts).get("clarification_requested")=="multiple_projects":
        initiate_gpt_query(user_query, assistant_name, channel_id, thread_ts, thread_context,"multiple_projects",user_slack_id, None)
        if message_ts:
            slack_client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=f"*{message_text}*",
                attachments=[]
            )
        return
    elif get_thread_metadata(thread_ts).get("clarification_requested")=="specific_project":
        initiate_gpt_query(user_query, assistant_name, channel_id, thread_ts, thread_context,"specific_project",user_slack_id, get_thread_metadata(thread_ts).get("project_name"))
        if message_ts:
            slack_client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=f"*{message_text}*",
                attachments=[]
            )
        return

    # If no clarification is requested, proceed with the query
    send_clarification_buttons(slack_client, channel_id, thread_ts)
    

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000, debug=True)