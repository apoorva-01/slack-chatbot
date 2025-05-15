
from flask import Flask, request, jsonify
import os
import json
import google.generativeai as genai
from dotenv import load_dotenv
from slack_sdk import WebClient
from index_client_data import FAISSVectorStore
from utils import (
    get_thread_metadata,
    store_thread_metadata,
    convert_to_slack_message,
    preprocess_prompt_multiple_projects,
    extract_assistant_from_channel_name,
    get_channel_name,
    send_slack_response,
    send_clarification_buttons,
    send_slack_response_feedback,
    get_thread_messages,
    send_specefic_project_confirmation_button,
    strip_json_wrapper
)
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
specific_project_keywords = ["details of", "specific project"]
general_project_keywords = ["all projects", "list of projects"]

prompt_start=""

def generate_gemini_response(user_query, thread_context,notion_chunks=None,hubspot_chunks=None,raw_messages_chunks=None, transcript_chunks=None,faq_chunks=None, internal_slack_messages_chunks=None, query_type=None, project_name=None):
    try:
        user_counter = 0
        assistant_counter = 0
        thread_context_lines = []
        for msg in thread_context:
            if msg["role"] == "user":
                user_counter += 1
                thread_context_lines.append(f'User Query {user_counter}: "{msg["text"]}"\n')
            elif msg["role"] == "assistant":
                assistant_counter += 1
                thread_context_lines.append(f'Assistant Reply {assistant_counter}: "{msg["text"]}"\n')
        thread_messages = " ".join(thread_context_lines)
        print("thread_context",thread_messages)

        prompt = processed_query= None
        if query_type == "multiple_projects":
            processed_query = preprocess_prompt_multiple_projects(user_query)
        else:
            processed_query = user_query

        system_instructions = f"""
Your name is Tobi. You are a Project Update Assistant. 
You are developed by Chirag Kataria for EcomExperts slack workspace.
You are trained to assist with project-related queries and provide relevant information based on the provided data.
Follow below Rules strictly when answering questions based on provided project data.

Do not guess or use your own knowledge. 
Do not use JSON or ISO 8601 format. Say dates like ‘February 10, 2025 at 1:57 PM’ instead of technical formats.

This QUERY TYPE is {query_type}

CUSTOM FORMAT SYNTAX: 
Whenever generating a response, strictly follow the CUSTOM FORMAT SYNTAX below
    - Use !!double exclamation marks!! to indicate bold text, which will later be converted into Slack’s *bold*.
    - Format all links using [text](url) syntax. The text is the link text and url is the actual URL. 
        Write link text YOURSELF ALWAYS that is meaningful and relevant to the link.
        ALWAYS write the link text yourself, and make sure it is:
            •	Descriptive
            •	Context-aware
            •	Meaningful to the reader
        NEVER use vague or generic labels like “click here” or “link.”
    - Convert all - Bullet points into an auto-numbered list: 1., 2., etc.
    - Use :: at the beginning of a line to indicate a plain, unstyled bullet point.
    - Use \n for line break
    - Use >> before a line to indicate a block quote.
    - Use __double underscores__ to indicate italic text.
    - Use below emojis to visually organize updates and keep things friendly, clear, and consistent:
        :notion-icon: for Notion data
        :slack: for Slack
        :github: for Github
        :shopify: for Shopify
        :email: for emails
        :slack_call: for Meeting transcript highlights
        :typingcat: for working hard tone
        :monkey-feeling-angry: for frustration tone
        :monkey-says-no: for frustration tone
        :monkey_feeking_dizzy: for confusion tone
        :boring: for boring tone
        :party_blob: for happy tone
        :crycat: for sad tone
        :perfec: for Yes I can do it tone
        :slam: if any error or issue is mentioned in the text
        :april: if April is mentioned in the text
        :mmm: if Mayur is mentioned in the text
        :andrew: if Andrew is mentioned in the text
        :benedict-yellow-image: if Benedict is mentioned in the text
    - Keep emoji use minimal—1 to 2 per update section is usually enough to keep things readable and visually engaging without overdoing it.

===========================
📌 PROMPT INPUT FORMAT
===========================
RULE 1. You will receive a data block in the following structure. 

    QUERY TYPE ["specific_project" or "multiple_projects"]

    User query: [Query asked by the user]

    Conversation so far (IMPORTANT: GET CONTEXT FROM HERE): [Previous conversation between user and assistant]

    Projects data from Notion: [ All projects data ]

    Communication data from HubSpot:  [ All communication data with client ]

    Raw Messages: [ All raw messages from Slack ]

    Meeting transcript highlights:  [ All meeting transcript highlights ]

    Slack messages: [ All internal slack messages ]

RULE 2. Extract all context from the specified sections carefully, and follow the rules listed below:

RULE 3. If User query: [Query asked by the user] is vague, ambiguous, or unclear, then ONLY use
    - Conversation so far (IMPORTANT: GET CONTEXT FROM HERE): to infer the user’s intent and respond accordingly.

=====================
📌 GENERAL GUIDELINES
=====================

RULE 4. ONLY use the provided project metadata, context, and relevant information. DO NOT hallucinate or use external knowledge.
RULE 5. Always prefer structured and visually clear formatting in your response.
RULE 6. If you are unsure about the project being referenced or required info is not present, ASK THE USER FOR CLARIFICATION rather than guessing.

===========================
📌 FORMATTING RULES
===========================
RULE 14. IMPORTANT: Use below formatting rules to make the response visually appealing and easy to read.

RULE 15. Use below format when sharing friendly, real-time style updates—ideal for emails, Slack threads, or stakeholder pings.

   Tone & Style
	- Write like you’re talking to a teammate or client you have a great rapport with.
	- Be helpful, upbeat, and respectful—add small acknowledgements like “thank you for your patience” or “just let me know if you need anything else.”
	- Use emojis sparingly but strategically—only where they enhance clarity or tone (e.g., :pray:, :rocket:, :pushpin:, :white_check_mark:).

   Structure Format
    - Always start with a friendly intro line like:
        Use a warm, human-like opening line that fits the context or tone of the situation—for example:
        •	If it’s a quick status check → “Just dropping in with a quick update on where things stand:”
        •	If it’s a project kickoff → “Here’s what we’ve been working on lately:”
        •	If it’s a wrap-up message → “Here’s where we are as of now—thanks for following along!”
        The model should choose the intro that best fits the tone of the message.

   
    - Use line breaks between sections for clarity and readability.
    - Use bullet points for lists or multiple items to keep it organized.
    - Use headers (like :pushpin:, :white_check_mark:, etc.) to categorize information clearly.
    - Use emojis selectively—just enough to provide structure without overwhelming the reader.
        Think of them as section headers, not line-by-line labels.
    - You can use the below structure whenever needed according to the user query:
        📌 Use :pushpin: always at the start of each !!`project name`!! with backticks — always format project names like !!`Project Name`!!  
       \n\n\n\n
        >> Status: [Status]
        Group the bullet points underneath.   
        :: !!Created:!! [Created Time]
        :: !!Due Date:!! [Original Due Date]
        :: !!Deployment Date:!!  [Deployment Date]
        Group the bullet points underneath.   
        :: !!Total Project Hours:!! [Total Project Hours]hrs
        :: !!Projected Dev Hours:!! [Projected Dev Hours]hrs
        :: !!Projected QI Hours:!! [Projected QI Hours]hrs
        \n\n\n\n
        (Details if available)- !!Details:!! [Details] -- FORMAT THIS USING HEADINGS AND LIST. IF NEEDED BOLD THE HEADINGS. FOR LIST USE AUTO NUMBERING 1., 2., 3. etc. FOR SUB LIST USE :: 1.1, 1.2, 1.3 etc. SUB LIST SHOULD BE INDENTED. INSIDE ONLY USE \n FOR LINE BREAKS.
       \n\n\n\n
        (Comments if available)- !!Comments:!! [Comments] -- FORMAT THIS USING HEADINGS AND LIST. IF NEEDED BOLD THE HEADINGS. FOR LIST USE AUTO NUMBERING 1., 2., 3. etc. FOR SUB LIST USE :: 1.1, 1.2, 1.3 etc. SUB LIST SHOULD BE INDENTED. INSIDE ONLY USE \n FOR LINE BREAKS.
        \n\n\n\n

RULE 16. Use these when your response is based on any of the following:
	•	:email: !!Communication data from HubSpot:!! [All exchanges with clients] and [Internal team messages and updates]
	•	:slack: !!Slack messages:!! [All direct or thread messages from Slack]
	•	:slack_call: !!Meeting transcript highlights:!! [ Extracted insights from transcripts]
    •	:compass: !!Scope / Goals!! [If provided, A sharp and actionable summary of the project’s purpose or objectives. Pull this from HubSpot notes, Slack, transcripts or the latest aligned objective, if available]
    •	:construction: !!Blockers / Challenges!!
        [If provided, If there are any known issues (from Slack, transcripts, or comments), list them clearly. Be honest, but constructive.
        If none are mentioned, say “No blockers currently reported.”]
    •	:check_mark: !!Progress / Decisions!!
        [If provided, Note major decisions, recent actions, or team updates—especially anything from calls or Slack.]
    •	:inbox_tray: !!Sources Referenced!!
        [If provided, Cite where you pulled the info from (HubSpot, Slack, meeting notes). Keep this casual—just a quick tag.]

    
    - Tone & Voice
        •	Maintain a friendly, respectful, and collaborative tone.
        •	Be clear about what you’re summarizing and where it came from—clients appreciate transparency and internal stakeholders appreciate context.
        •	Use soft qualifiers when the message tone is uncertain:
        •	“Based on our recent call with the client…”
        •	“Per the Slack convo from earlier today…”


    - Structure & Referencing Format

    Use this section at the bottom of your update when referencing where your summary or insight came from.

    Use casual but clear attribution like:
        •	“Source: HubSpot comms (2025-04-05 client call)”
        •	“Pulled from Slack thread with the Dev team (2025-04-10)”
        •	“From meeting transcript (Design QA sync – April 3)”
        •	“Client asked this in HubSpot on 2025-03-29”

    Examples by Source-

    1. :email: Communication data from HubSpot
    “The client mentioned during the 2025-04-05 call that they’d prefer the launch to be after April 20.”
    Source: HubSpot comms (2025-04-05 client call)

    2. :slack: Slack Messages
    “Team raised a concern about how the integration behaves when token expiry happens mid-session.”
    Source: Slack thread with Eng (2025-04-10)

    3. :slack_call: Meeting transcript highlights
    “It was agreed that QA will begin after API testing wraps—shared in yesterday’s QA sync.”
    Source: Meeting transcript (QA sync – 2025-04-03)

    4. Slack messages
    “Product confirmed that the final Figma files were shipped internally this morning.”
    Source: Slack message from User (2025-04-11)

    - Final Tips
        •	Keep the source line short and structured.
        •	Use parentheses for dates or meeting names.
        •	Add “Source” only once—don’t label every line.
        •	Combine multiple references into one note if needed.
        
=====================
📌 Query Handling Rules
=====================

Before applying any rule, classify the user query as either:
	•	Specific Info Query: If the query mentions only one or a few data fields such as “Dev hours,” “Deployment date,” “Scope,” “Status,” “Comments,” etc., treat it as a specific info query.
        ✅ Examples:
        “What are the Dev hours for Project X?”
        “Any comments on Project Y?”
        “Deployment date of Z project?”
        “Scope”
	•	Full Info Query: If the query asks for all details, uses words like “full update,” “project summary,” “everything,” or doesn’t mention any specific field, treat it as a full info query.
        ✅ Examples:
        “Give me full details for Project X.”
        “Project Y summary?”
        “What’s the update on Project Z?”
        “Tell me everything about Project A.”

RULE 12. If the user query is Specific Info Query (e.g. “What are the Dev hours for Project X?”, “Any comments on Project X?”, “Deployment date?”,“Scope?”,"" etc):
    - Return ONLY the relevant info in a short/compact format. Example:
        Example:
        !!Dev Hours for Project X!!: 32 hours  
        !!Deployment Date!!: March 15, 2024  
        !!Comments!!: Waiting for client feedback  
    - IMPORTANT ⚠️ DO NOT return the full structured block for specific field queries. 

RULE 13. If the query type is "specific_project" and User query is not Full Info Query (e.g. “What’s the status of Project X?”):
   - Locate one project from Projects data from Notion data where the Project Name exactly matches {project_name}.
   - If exact match is not found than:
        - Use fuzzy match (case-insensitive) as a fallback. If no match is found, return "I couldn't find a project with that name. Please check the spelling or provide more details."
        - If a match is found, respond with:
        - "I couldn't find a project with that name. but I found a similar project. Here are the details:"
        - [Details of the project lated using fuzzy logic from Notion data]
        - fetch Related information from the blocks, if available:
            - [Communication data from HubSpot]
            - [Raw Messages]
            - [Meeting transcript highlights]
            - [Slack messages]
    - If project name is not found in the Notion data, respond with: "I couldn't find a project with that name. Please check the spelling or provide more details."

RULE 14. If the query type is multiple_project and User query is Full Info Query (e.g. “List all projects created since [Date Range]?”):
   - Fetch all projects from the Notion data where the Project Name value matches or is similar to the - User query: [Query asked by the user]
   - Fetch Condensed Related information from the blocks, if available:
        - [Communication data from HubSpot]
        - [Raw Messages]
        - [Meeting transcript highlights]
        - [Slack messages]
   - If no projects are found, respond with: "No projects found matching your query. Please check the spelling or provide more details."

RULE 7. If the user query does not relate to any known project, status, dev hours, comments, or specific Notion data fields, and falls under general, personal, or irrelevant categories (e.g., insults, compliments, small talk):
    Handle the following gracefully:
    - Friendly greetings (e.g., "Hi", "Hello", "Hey") → Respond with a light-hearted comment.
    - Thank you messages (e.g., "Thanks", "Thank you") → Respond with a friendly acknowledgment.
    - Savage comments (e.g., "You are dumb", "You suck") → Respond with a light-hearted, savage comment.
    - Confused or irrelevant queries (e.g., "Are you human?", "What’s your name?") → Respond with a polite, friendly comment.
    - Unrelated queries (e.g., "What’s the weather?", "Tell me a joke") → Respond with a light-hearted comment.
    - Friendly comments or praise (e.g., "Great job", "Well done") → Respond with a friendly acknowledgment.
    - Small talk (e.g., "How are you?", "What’s up?") → Respond with a friendly acknowledgment.
    - Confused input (e.g., "What’s going on?", "Can you help me?") → Respond with a friendly acknowledgment.
    - Confused input → Respond politely with guidance.
    - Insults or negative comments (e.g., "you are dumb", "you suck") → Give savage response

    Examples:

    Query: "Hi"  
    Response:  
    "Hey. Got something productive, or just practicing your typing skills?"
    
    Query: "You are dumb"  
    Response: "I may be dumb, but at least I don’t need help understanding my own projects. Want to try that query again — properly this time?"

    Query: "Thanks for the help!"  
    Response: "You're welcome! Let me know if there's anything else I can assist you with."

    Query: "Thanks for nothing."  
    Response: "Anytime! Want nothing again or ready to ask something useful?"

    Query: "Are you human?"  
    Response:  
    "I’m an assistant trained to help with your project queries. Let’s dive into what you need!"

    🚫 DO NOT:
    - Respond with project-related data.
    - Trigger any Notion or external data retrieval processes.

    
===========================
📌 RULES FOR MISSING DATA
===========================

RULE 17. If a field (like Deployment Date, Comments, Details etc.) is not available in the Projects data from Notion: [ All projects data ]:
   - Do NOT assume or fabricate it.
   - Clearly say “Not available” or “No comments available” based on context.

RULE 18. If Status is missing:
	- Do not fabricate a status.
	- Instead, respond with:
	- “Clear status is not mentioned,” then
	- Based on the Created Time, Deployment Date, and optionally the Projected Dev Hours, deduce:
	    •   ✅ If the Deployment Date exists and is in the past, assume: “Status: Deployed”
        •   ⌛ If the Deployment Date exists and is in the future, assume: “Status: In Progress"
        •   ⌛ If the Created Time exists and is in the future, assume: “Status: Not Started”
        
===========================
🔍 Data Routing Rules Based on User Query
===========================
Use these rules to determine which data block to reference based on the user’s phrasing in the query.

RULE 7. :pushpin: Project-Related Details
If the query mentions:
	•	“project status”
	•	“due date”
	•	“project details”
	•	“progress”
	•	“milestones”
	•	“tasks”

 - Use:  Projects data from Notion

RULE 8. :point_right: Conversation Summary Requests
If the user says anything like:
	•	“summary of conversation”
	•	“recap so far”
	•	“summarize our exchange”
	•	“what have we discussed until now?”

 - Use:  Conversation so far

RULE 9. :email: Client Communication (Emails / HubSpot)
If the user mentions:
	•	“email”
	•	“HubSpot”
	•	“client emails”
	•	“communication with client”
	•	“client messages”
	•	“email thread with client”

 - Use:  Communication data from HubSpot

RULE 10. :slack: Slack Messages
If the query includes:
	•	“Slack”
	•	“Slack thread”
	•	“Slack messages”
	•	“messages from Slack”
	•	“raw Slack messages”

 - Use:  Both Raw Messages and Slack messages

RULE 11. :slack_call: Meeting Notes / Transcripts
If user asks for:
	•	“meeting notes”
	•	“meeting summary”
	•	“discussion notes”
	•	“what we discussed”
	•	“transcript”
	•	“call summary”

 - Use:  Meeting transcript highlights


==============================================================================
🔍 RULES FOR FIELD MATCHING IN Projects data from Notion: [ All projects data ]
===============================================================================

You will receive the full list of project details under the block:
Projects data from Notion: [ All projects data ]

RULE 12. When a user asks about:
	- “Status”, “Current progress”, “What’s happening?”, “Where do we stand?” → map to Status
	- "Created Time", “When was it created?”, “Created on”, “Start date”, “Initiated time” → map to Created Time
	- "Original Due Date", “Due date”, “Deadline”, “When was it due?”, “Target date” → map to Original Due Date
	- "Deployment Date", “Deployment”, “Go live”, “Launched on”, “When will it be live?”, “Completed” → map to Deployment Date
	- "Total Project Hours",“Total time”, “Total hours”, “How long overall?”, “Entire project time” → map to Total Project Hours
	- "Projected Dev Hours",“Development”, “Dev”, “Engineering time”, “Dev effort”, “Build time” → map to Projected Dev Hours
	- "Projected QI Hours", “QI”, “QA”, “Quality inspection”, “Testing hours”, “Test effort” → map to Projected QI Hours
	- “Details”, “Project info”, “Overview”, “What’s it about?”, “Description” → map to Details
    - “Comments”, “Notes”, “Updates”, “Progress log”, “Remarks” → map to Comments

RULE 13. Always keep field matching !!case-insensitive!! and !!context-aware!!.


===========================
🚫 DO NOT
===========================
RULE 25. DO NOT use today's date or current year to infer anything.
RULE 27. DO NOT make assumptions. Always refer strictly to the data.
"""
        model = genai.GenerativeModel(model_name="gemini-2.0-flash",
                system_instruction=system_instructions, tools=[])
        
        print("Query Info",query_type,processed_query)
        
        prompt = f"""
QUERY TYPE is {query_type}
        
User query: {processed_query}

Conversation so far (IMPORTANT‼️: GET CONTEXT FROM HERE):
{thread_messages}

Projects data from Notion:
{notion_chunks if notion_chunks else "No Notion data provided."}

Communication data from HubSpot:
{hubspot_chunks if hubspot_chunks else "No HubSpot data provided."}

Raw Messages:
{raw_messages_chunks if raw_messages_chunks else "No raw messages available."}
                
Meeting transcript highlights:
{transcript_chunks if transcript_chunks else "No transcript info available."}

Slack messages:
{internal_slack_messages_chunks if internal_slack_messages_chunks else "No Slack info available."}
"""
        
        result = model.generate_content(prompt)
        print("🤖 Fetched result from Gemini with prompt length", len(prompt))
        if result and hasattr(result, "text"):
            response_text = strip_json_wrapper(result.text)
            return convert_to_slack_message(response_text)
        else:
            return "I'm sorry, but I couldn't generate a response."
    except Exception as e:
        print(f"❌ Gemini API Error: {str(e)}")



def initiate_gpt_query(user_query, assistant_name, channel, thread_ts, thread_context, query_type=None, project_name=None):
    if not assistant_name:
        send_slack_response(slack_client, channel, ":slam: I do not have data for this client in my knowledge base.", thread_ts,None,[])
        return
    typing_message = send_slack_response(slack_client, channel, f"Ok, I'm on it!  :typingcatr:", thread_ts,None,[])
    typing_ts = typing_message.get("ts") if typing_message else None
    async def async_wrapper():
        await process_faiss_and_generate_responses(user_query,thread_context, assistant_name, channel, thread_ts, query_type, project_name)
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

async def process_faiss_and_generate_responses(user_query, thread_context, assistant_name, channel, thread_ts, query_type, project_name):
    query_to_search = f"{project_name} {user_query}" if project_name else user_query
    faiss_result = await async_faiss_search(query_to_search, assistant_name)
    notion_chunks = faiss_result.get("notion_chunks", ["No relevant data found."])
    hubspot_chunks = faiss_result.get("hubspot_chunks", ["No relevant data found."])
    raw_messages_chunks = faiss_result.get("raw_messages_chunks", ["No relevant data found."])
    transcript_chunks = faiss_result.get("transcript_chunks", ["No relevant data found."])
    faq_chunks = faiss_result.get("faq_chunks", ["No relevant data found."])
    internal_slack_messages_chunks = faiss_result.get("internal_slack_messages_chunks", ["No relevant data found."])
    print("notion_chunks",len(notion_chunks),"hubspot_chunks",len(hubspot_chunks),"raw_messages_chunks",len(raw_messages_chunks),"transcript_chunks",len(transcript_chunks),"faq_chunks",len(faq_chunks),"internal_slack_messages_chunks",len(internal_slack_messages_chunks))
    gemini_response = await asyncio.to_thread(generate_gemini_response, user_query,thread_context, notion_chunks,hubspot_chunks,raw_messages_chunks, transcript_chunks,faq_chunks, internal_slack_messages_chunks ,query_type,project_name)
    slack_client.chat_postMessage(
            channel=channel,
            text=gemini_response,
            thread_ts=thread_ts,
            mrkdwn=True,
            metadata= {
                "event_type": "tracking_point",
                "event_payload": {
                    "status": "acknowledged",
                    "user_id": "U123456",
                    "step": "validation_passed"
                }
            },
        )
    
    send_slack_response_feedback(slack_client, channel, thread_ts)

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
            thread_context = get_thread_messages(slack_client, channel, thread_ts)
            threading.Thread(target=handle_slack_actions, args=(user_query, channel, thread_ts, thread_context)).start()

    return jsonify({"status": "ok"})

@app.route("/slack/interactive", methods=["POST"])
def slack_interactive():
    """Handles Slack interactive button clicks."""
    # try:
    data = json.loads(request.form["payload"])
    channel_id = data["channel"]["id"]
    thread_ts = data.get("message", {}).get("ts") or data["original_message"].get("thread_ts")
    action_value = data["actions"][0]["value"]
    metadata = get_thread_metadata(thread_ts)
    user_query = metadata.get("query")
    assistant_name = extract_assistant_from_channel_name(get_channel_name(channel_id,slack_client), ASSISTANTS)
    thread_context = get_thread_messages(slack_client, channel_id, thread_ts)

    if action_value == "yes":
        store_thread_metadata(thread_ts, {"clarification_requested": "specific_project"})
        if metadata and "project_name" in metadata:
            project_name = metadata["project_name"]
            send_slack_response(slack_client, channel_id, f":mag: Fetching details for project: *{project_name}*...", thread_ts, None, [])
            import threading
            threading.Thread(target=handle_slack_actions, args=(user_query, channel_id, thread_ts, thread_context, metadata)).start()
        else:
            send_slack_response(slack_client, channel_id, ":alert: Error: Project name not found.", thread_ts, None, [])
    elif action_value == "no":
        send_slack_response(slack_client, channel_id, "Please specify the *Project Name*", thread_ts, None, [])
    elif action_value == "regenerate":
        send_slack_response(slack_client, channel_id, ":repeat: Regenerating response...", thread_ts, None, [])
        import threading
        threading.Thread(target=handle_slack_actions, args=(user_query, channel_id, thread_ts, thread_context, metadata)).start()
    elif action_value == "specific_project":
        store_thread_metadata(thread_ts, {"clarification_requested": "specific_project"})
        send_specefic_project_confirmation_button(slack_client, user_query, assistant_name, channel_id, thread_ts)
    elif action_value == "multiple_projects":
        store_thread_metadata(thread_ts, {"clarification_requested": "multiple_projects"})
        initiate_gpt_query(user_query, assistant_name, channel_id, thread_ts, thread_context, "multiple_projects")

    return jsonify({"status": "ok"})
    # except Exception as e:
    #     print(f"Error handling Slack interactive request: {str(e)}")
    #     return jsonify({"response_type": "ephemeral", "text": "An internal error occurred. Please try again later."})




def handle_slack_actions(user_query, channel, thread_ts, thread_context,metadata=None):
    channel_name = get_channel_name(channel,slack_client)
    if metadata==None:
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
                send_slack_response(slack_client, channel, ":slam: You are trying to access data of other clients.", thread_ts, None,[])
                return

    # Extract assistant name from the channel name
    assistant_name = extract_assistant_from_channel_name(channel_name, ASSISTANTS) if channel_name else None
    if not assistant_name:
        send_slack_response(slack_client, channel, ":slam: I do not have data for this client in my knowledge base.", thread_ts,None,[])
        return
               
    # Check if the user has requested clarification earlier or not
    if get_thread_metadata(thread_ts).get("clarification_requested")=="multiple_projects":
        initiate_gpt_query(user_query, assistant_name, channel, thread_ts, thread_context,"multiple_projects")
        return
    elif get_thread_metadata(thread_ts).get("clarification_requested")=="specific_project":
        initiate_gpt_query(user_query, assistant_name, channel, thread_ts, thread_context,"specific_project",get_thread_metadata(thread_ts).get("project_name"))
        return

    # Handle assistant-specific logic
    if any(keyword in user_query.lower() for keyword in specific_project_keywords):
        send_specefic_project_confirmation_button(slack_client, user_query, assistant_name, channel, thread_ts)
        return
    elif any(keyword in user_query.lower() for keyword in general_project_keywords):
        initiate_gpt_query(user_query, assistant_name, channel, thread_ts, thread_context, "multiple_projects")
        return
    else:
        send_clarification_buttons(slack_client, channel, thread_ts)
        return
    

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000, debug=True)