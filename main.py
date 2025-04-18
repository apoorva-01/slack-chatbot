from flask import Flask, request, jsonify, make_response
import os
import json
import google.generativeai as genai
from dotenv import load_dotenv
from slack_sdk import WebClient
from index_client_data import FAISSVectorStore
from utils import (
    get_thread_metadata,
    store_thread_metadata,
    extract_assistant_from_channel_name,
    clean_slack_formatting,
    get_channel_name,
    send_slack_response,
    send_clarification_buttons,
    send_slack_response_feedback,
    get_thread_messages,
    send_specific_project_confirmation_button,
)

from gemini_utils import (
    classify_multiple_projects_query_intent,
    get_multiple_projects_from_thread_context,
    generate_gemini_response,
    generate_custom_filter_response
)

import asyncio
import threading
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS_JSON")
SLACK_BOT_USER_ID = os.getenv("SLACK_BOT_USER_ID")

# Initialize services
faiss_store = FAISSVectorStore()  # Initialize FAISS vector store for data indexing and retrieval
app = Flask(__name__)  # Initialize Flask application
genai.configure(api_key=GEMINI_API_KEY)  # Configure Generative AI with API key
slack_client = WebClient(token=SLACK_BOT_TOKEN)  # Initialize Slack WebClient with bot token

# List of assistants derived from FAISS index files
ASSISTANTS = [f.replace(".faiss", "") for f in os.listdir("faiss_index") if f.endswith(".faiss")]


def generate_final_response(user_query, is_follow_up, thread_context, thread_messages, notion_chunks=None, hubspot_chunks=None, raw_messages_chunks=None, transcript_chunks=None, faq_chunks=None, internal_slack_messages_chunks=None, query_type=None, user_slack_id=None, project_name=None, multiple_projects_array=None):
    """
    Generates the final response based on the user query, context, and data chunks.

    Args:
        user_query (str): User's query.
        is_follow_up (bool): Indicates if the query is a follow-up.
        thread_context (list): Conversation thread context.
        thread_messages (str): Conversation messages.
        notion_chunks, hubspot_chunks, raw_messages_chunks, transcript_chunks, faq_chunks, internal_slack_messages_chunks (list): Data chunks for context.
        query_type (str, optional): Type of the query.
        user_slack_id (str, optional): Slack user ID.
        project_name (str, optional): Specific project name.
        multiple_projects_array (list, optional): List of multiple project names.

    Returns:
        str: Final response or error message.
    """
    try:
        result = None
        print("is_follow_up", is_follow_up)
        if query_type == "multiple_projects":
            #  classify query intent
            if classify_multiple_projects_query_intent(user_query):
                print("🔎 Classified as filtering query")
                result = generate_custom_filter_response(user_query, notion_chunks)
            else:
                print("💬 Classified as non-filtering query")
                user_query_with_project_context = user_query
                if is_follow_up and len(multiple_projects_array)>0:
                    user_query_with_project_context = f"""{user_query}
- Use projects in {multiple_projects_array} to answer the user query, unless a project name is explicitly entered."""
                
                print(user_query_with_project_context,"user_query_with_project_context")
                result = generate_gemini_response(
                        query_type, user_query_with_project_context, thread_messages, notion_chunks, hubspot_chunks,
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
        return "An error occurred while generating the final response."


def initiate_gpt_query(user_query, assistant_name, channel, thread_ts, thread_context, query_type=None, user_slack_id=None,project_name=None):
    """
    Initiates a GPT query asynchronously.

    Args:
        user_query (str): User's query.
        assistant_name (str): Name of the assistant.
        channel (str): Slack channel ID.
        thread_ts (str): Thread timestamp.
        thread_context (list): Conversation thread context.
        query_type (str, optional): Type of the query.
        user_slack_id (str, optional): Slack user ID.
        project_name (str, optional): Specific project name.
    """
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
    """
    Processes FAISS search and generates responses asynchronously.

    Args:
        user_query (str): User's query.
        thread_context (list): Conversation thread context.
        assistant_name (str): Name of the assistant.
        channel (str): Slack channel ID.
        thread_ts (str): Thread timestamp.
        query_type (str): Type of the query.
        user_slack_id (str): Slack user ID.
        project_name (str): Specific project name.
    """
    try:
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
    except Exception as e:
        print(f"❌ Error in process_faiss_and_generate_responses: {str(e)}")
        send_slack_response(slack_client, channel, "An error occurred while processing your request.", thread_ts, None, [])

async def async_faiss_search(full_prompt, assistant_name,channel,thread_ts):
    """
    Performs an asynchronous FAISS search.

    Args:
        full_prompt (str): Full prompt for the search.
        assistant_name (str): Name of the assistant.
        channel (str): Slack channel ID.
        thread_ts (str): Thread timestamp.

    Returns:
        dict: FAISS search results.
    """
    try:
        return await asyncio.to_thread(faiss_store.search_faiss, full_prompt, assistant_name)
    except Exception as e:
        print(f"❌ FAISS Error: {e}")
        send_slack_response(slack_client,channel,"Hey <@U08B0GKSTGF>, I’m broken 🫠 Got a query indexing error... fix me fast, I have work to do :typingcat:", thread_ts, None, [])
        return {"notion_chunks": [], "hubspot_chunks": [], "raw_messages_chunks": [], "transcript_chunks": [], "faq_chunks": [], "internal_slack_messages_chunks": []}

@app.route("/slack/events", methods=["POST"])
def slack_events():
    """    Handles Slack events.    """
    try:
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
                user_query = clean_slack_formatting(event["text"].replace(f"<@{SLACK_BOT_USER_ID}>", "").strip())
                thread_context = get_thread_messages(slack_client, channel, thread_ts)
                threading.Thread(target=handle_slack_actions, args=(user_query, channel, thread_ts, thread_context, user_slack_id)).start()

        return make_response(jsonify({"status": "ok"}), 200)
    except Exception as e:
        print(f"❌ Error in slack_events: {str(e)}")
        return make_response(jsonify({"status": "error", "message": str(e)}), 500)

@app.route("/slack/interactive", methods=["POST"])
def slack_interactive():
    """
    Handles Slack interactive button clicks.

    Returns:
        Response: JSON response indicating the status.
    """
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
        print(f"❌ Error handling Slack interactive request: {str(e)}")
        return make_response(jsonify({"status": "Error", "message": str(e)}), 500)


def handle_slack_actions(user_query, channel_id, thread_ts, thread_context,user_slack_id,metadata=None,message_ts=None,message_text=None):
    """
    Handles Slack actions based on user input.

    Args:
        user_query (str): User's query.
        channel_id (str): Slack channel ID.
        thread_ts (str): Thread timestamp.
        thread_context (list): Conversation thread context.
        user_slack_id (str): Slack user ID.
        metadata (dict, optional): Metadata for the thread.
        message_ts (str, optional): Message timestamp.
        message_text (str, optional): Message text.
    """
    try:
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
    except Exception as e:
        print(f"❌ Error in handle_slack_actions: {str(e)}")
        send_slack_response(slack_client, channel_id, "An error occurred while handling Slack actions.", thread_ts, None, [])


if __name__ == "__main__":
    try:
        app.run(host="0.0.0.0", port=3000, debug=True)
    except Exception as e:
        print(f"❌ Error starting the application: {str(e)}")