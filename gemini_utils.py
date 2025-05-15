

import os
from dotenv import load_dotenv
import json
import google.generativeai as genai
load_dotenv()

from system_instruction import get_system_instructions
from filter_logic import (
    generate_gemini_parsed_query,
    convert_parsed_query_to_filter,
    query_notion_projects,
    format_multiple_projects_flash_message
)
from utils import (
    convert_to_slack_message,
    strip_json_wrapper
)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)

def classify_multiple_projects_query_intent(user_query):
    prompt = f"""
    You are a classification assistant. Decide if the following user query is a filtering-based query.

    Examples of filtering queries:
    - Show only completed projects.
    - List projects from last month.
    - Filter projects by team member.
    - Projects where status is 'In Progress'.

    Examples of non-filtering queries:
    - Summarize updates for this project.
    - What are the key blockers for projects?
    - Provide a summary of the last meeting.
    - What are the main issues with the project?
    - give details of all the call meetings we have
    - What are the main takeaways from the last meeting?
    - Give all related slack messages
    - How are clients responding?
    - What’s the overall sentiment?

    Query:
    "{user_query}"

    Is this query a filtering-based query? Answer with only 'Yes' or 'No'.
    """

    model = genai.GenerativeModel(model_name="gemini-2.0-flash")
    response = model.generate_content(prompt)
    return response.text.strip().lower().startswith("yes")


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
        # Initialize generative model with instructions
        model = genai.GenerativeModel(model_name="gemini-2.0-flash", system_instruction=system_instructions, tools=[])
        # Prepare the prompt with the thread context
        prompt = f"Here is the conversation:\n{thread_context}\n\nReturn a JSON array of all project names mentioned."
        result = model.generate_content(prompt)

        if result and hasattr(result, "text"):
            try:
                # Clean and parse the response from the model
                cleaned_response = result.text.strip()
                if cleaned_response.startswith("```json"):
                    cleaned_response = cleaned_response[7:].strip()  # Remove ```json prefix
                if cleaned_response.endswith("```"):
                    cleaned_response = cleaned_response[:-3].strip()  # Remove ``` suffix

                # Parse JSON response into a Python list
                project_names = json.loads(cleaned_response)
                return project_names  # Return as a Python list
            except json.JSONDecodeError as e:
                # Handle JSON parsing errors
                print(f"❌ Failed to decode JSON response: {e}")
                return []
        else:
            # Handle cases where the model does not return a valid response
            print("❌ No valid response from the model.")
            return []
    except Exception as e:
        # Handle unexpected errors during the process
        print(f"❌ Gemini API Error: {str(e)}")
        return []
    


def generate_gemini_response(query_type, processed_query, thread_messages, notion_chunks, hubspot_chunks, raw_messages_chunks, transcript_chunks, faq_chunks, internal_slack_messages_chunks, project_name=None, multiple_projects_array=None):
    """
    Generates a response using the generative AI model based on the provided query and context.

    Args:
        query_type (str): Type of the query (e.g., "multiple_projects").
        processed_query (str): Processed user query.
        thread_messages (str): Conversation context.
        notion_chunks, hubspot_chunks, raw_messages_chunks, transcript_chunks, faq_chunks, internal_slack_messages_chunks (list): Data chunks for context.
        project_name (str, optional): Specific project name.
        multiple_projects_array (list, optional): List of multiple project names.

    Returns:
        str: Generated response or error message.
    """
    try:
        if multiple_projects_array:
            print("multiple_projects_array", multiple_projects_array)
        # Generate system instructions for the model
        system_instructions = get_system_instructions(query_type, False, processed_query, project_name, multiple_projects_array)
        # Initialize generative model
        model = genai.GenerativeModel(model_name="gemini-2.0-flash", system_instruction=system_instructions, tools=[])
        # Prepare the prompt with all relevant context
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
        # Generate content using the model
        result = model.generate_content(prompt)
        if result and hasattr(result, "text"):
            # Process and return the response
            response_text = strip_json_wrapper(result.text)
            return convert_to_slack_message(response_text)
        else:
            # Handle cases where the model does not return a valid response
            print("❌ No valid response from the model.")
            return "I'm sorry, but I couldn't generate a response."
    except Exception as e:
        # Handle unexpected errors during the process
        print(f"❌ Error in generate_gemini_response: {str(e)}")
        return "An error occurred while generating a response."

def generate_custom_filter_response(user_query, notion_chunks):
    """
    Generates a custom filter response based on the user query and Notion data.

    Args:
        user_query (str): User's query.
        notion_chunks (list): Data chunks from Notion.

    Returns:
        str: Filtered response or error message.
    """
    try:
        parsed_query = generate_gemini_parsed_query(user_query)
        notion_filter = convert_parsed_query_to_filter(parsed_query)
        print("notion_filter", notion_filter)
        matching_projects = query_notion_projects(notion_filter, notion_chunks)
        print("matching_projects", len(matching_projects))
        if not matching_projects:
            return "No projects found matching that criteria 😕"
        return convert_to_slack_message(format_multiple_projects_flash_message(matching_projects, parsed_query))
    except Exception as e:
        print(f"❌ Error in generate_custom_filter_response: {str(e)}")
        return "An error occurred while generating a custom filter response."
              