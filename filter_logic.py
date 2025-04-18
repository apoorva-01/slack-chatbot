import os
import re
import json
import google.generativeai as genai
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime
from utils import (
    preprocess_relative_dates
)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)



def generate_gemini_parsed_query(user_query):
    field_keys = [
        "project_name", "created_time", "original_due_date", "deployment_date",
        "total_hours", "dev_hours", "qi_hours", "details", "comments"
    ]

    allowed_operators = ["equals", "contains", "before", "after", "greater_than", "less_than", "between", "in","not_equals", "not_contains"]
    normalized_query = preprocess_relative_dates(user_query)
    print(f"Normalized query: {normalized_query}")

    current_year = datetime.now().year

    # Updated system instruction
    system_instruction = f"""You are an intelligent project query parser.

Your task is to convert natural language queries into structured JSON filters using specific fields and operators.

📌 Rules to follow:
1. Return only a dictionary with keys from this list: {field_keys}
2. Use only the following comparison operators: {allowed_operators}
3. Dates must use `"before"`, `"after"`, `"between"`, or `"in"` with ISO date format (`YYYY-MM-DD`)
4. Numbers use `"greater_than"`, `"less_than"`, or `"between"`
5. Text uses `"equals"` or `"contains"`
6. ❗ If a **date is mentioned without a year**, assume the year is **{current_year}**
"""

    prompt = f"""
{system_instruction}

### Field Mapping:
- "Created Time", "When was it created?", "Created on", "Start date", "Initiated time" → **created_time**
- "Original Due Date", "Due date", "Deadline", "When was it due?", "Target date" → **original_due_date**
- "Deployment Date", "Deployment", "Go live", "Launched on", "When will it be live?", "Completed" → **deployment_date**
- "Total Project Hours", "Total time", "Total hours", "How long overall?", "Entire project time" → **total_hours**
- "Projected Dev Hours", "Development", "Dev", "Engineering time", "Dev effort", "Build time" → **dev_hours**
- "Projected QI Hours", "QI", "QA", "Quality inspection", "Testing hours", "Test effort" → **qi_hours**
- "Details", "Project info", "Overview", "What's it about?", "Description" → **details**
- "Comments", "Notes", "Updates", "Progress log", "Remarks" → **comments**

Example input query:
"Show me all projects deployed after March 1st with more than 2 hours of dev work"

Example output:
{{
  "deployment_date": {{"after": "{current_year}-03-01"}},
  "dev_hours": {{"greater_than": 2}}
}}

Example input query:
"Show me all projects created in March"
Example output:
{{
  "deployment_date": {{"in": ["{current_year}-03-01","{current_year}-03-31"]}}
  }}

Now parse the following query and return a valid JSON dictionary using these rules:

Query: "{normalized_query}"
"""

    model = genai.GenerativeModel(model_name="gemini-2.0-flash",system_instruction=system_instruction, tools=[])
    result = model.generate_content(prompt)
    response_text = result.text.strip()

    if response_text.startswith("```json"):
        response_text = response_text.strip("```json").strip("```").strip()

    try:
        return json.loads(response_text)
    except Exception:
        return {"raw_response": response_text, "error": "Failed to parse as JSON"}
    
def convert_parsed_query_to_filter(parsed_query):
    filters = []
    print(f"Parsed query: {parsed_query}")

    # Special case for project_name with custom filter
    if "project_name" in parsed_query:
        match_value = parsed_query["project_name"]
        if isinstance(match_value, dict) and "equals" in match_value:
            match_value = match_value["equals"]
        if isinstance(match_value, dict) and "contains" in match_value:
            match_value = match_value["contains"]
        filters.append({
            "property": "Project Name",
            "rich_text": {"project_match": match_value}
        })
    # Mapping of fields to their Notion properties and types
    field_map = {
        "project_name": ("Project Name", "rich_text"),  # still used for consistency
        "created_time": ("Created Time", "date"),
        "original_due_date": ("Original Due Date", "date"),
        "deployment_date": ("Deployment Date", "date"),
        "total_hours": ("Total Project Hours", "number"),
        "dev_hours": ("Projected Dev Hours", "number"),
        "qi_hours": ("Projected QI Hours", "number"),
        "details": ("Details", "rich_text"),
        "comments": ("Comments", "rich_text"),
        "status": ("Status", "select"),
    }

    for key, (notion_prop, notion_type) in field_map.items():
        if key not in parsed_query or key == "project_name":
            continue  # Skip if key not present OR it's already handled

        value = parsed_query[key]

        if notion_type == "number":
            if isinstance(value, dict):
                for operator, val in value.items():
                    filters.append({
                        "property": notion_prop,
                        "number": {operator: val}
                    })
            else:
                filters.append({
                    "property": notion_prop,
                    "number": {"equals": value}
                })

        elif notion_type == "select":
            filters.append({
                "property": notion_prop,
                "select": {"equals": value}
            })

        elif notion_type == "date":
            if isinstance(value, dict):
                if "between" in value and isinstance(value["between"], list) and len(value["between"]) == 2:
                    start, end = value["between"]
                    filters.append({
                        "property": notion_prop,
                        "date": {
                            "on_or_after": start,
                            "on_or_before": end
                        }
                    })
                if "in" in value and isinstance(value["in"], list) and len(value["in"]) == 2:
                    start, end = value["in"]
                    filters.append({
                        "property": notion_prop,
                        "date": {
                            "on_or_after": start,
                            "on_or_before": end
                        }
                    })
                if "since" in value:
                    filters.append({
                        "property": notion_prop,
                        "date": {"on_or_after": value["since"]}
                    })
                if "before" in value:
                    filters.append({
                        "property": notion_prop,
                        "date": {"before": value["before"]}
                    })
                if "after" in value:
                    filters.append({
                        "property": notion_prop,
                        "date": {"after": value["after"]}
                    })
                if "equals" in value:
                    filters.append({
                        "property": notion_prop,
                        "date": {"equals": value["equals"]}
                    })

        elif notion_type == "rich_text":
            if isinstance(value, dict):
                filters.append({
                    "property": notion_prop,
                    "rich_text": value
                })
            else:
                filters.append({
                    "property": notion_prop,
                    "rich_text": {"contains": value}
                })

    return {"and": filters} if filters else {}

def query_notion_projects(filters, all_projects_data):
    def match_condition(value, condition):
        if isinstance(condition, dict):
            for op, target in condition.items():
                if op == "equals":
                    if value != target:
                        return False
                elif op == "not_equals":
                    if value == target:
                        return False
                elif op == "contains":
                    if target.lower() not in (value or "").lower():
                        return False
                elif op == "not_contains":
                    if target.lower() in (value or "").lower():
                        return False
                elif op == "before":
                    try:
                        if not value or datetime.fromisoformat(value) >= datetime.fromisoformat(target):
                            return False
                    except:
                        return False
                elif op == "after":
                    try:
                        if not value or datetime.fromisoformat(value) <= datetime.fromisoformat(target):
                            return False
                    except:
                        return False
                elif op == "greater_than":
                    if not value or float(value) <= float(target):
                        return False
                elif op == "less_than":
                    if not value or float(value) >= float(target):
                        return False
                elif op == "on_or_after":
                    try:
                        if not value or datetime.fromisoformat(value) < datetime.fromisoformat(target):
                            return False
                    except:
                        return False
                elif op == "on_or_before":
                    try:
                        if not value or datetime.fromisoformat(value) > datetime.fromisoformat(target):
                            return False
                    except:
                        return False
                elif op == "project_match":
                    return findMatches(value, target)
        return True

    def project_matches(project, conditions):
        for cond in conditions:
            prop = cond["property"]
            condition = list(cond.values())[1]  # skip "property" key
            project_value = project.get(prop)
            if not match_condition(project_value, condition):
                return False
        return True

    and_filters = filters.get("and", [])
    matching_projects = [
        project for project in all_projects_data
        if project_matches(project, and_filters)
    ]
    return matching_projects

def infer_status(project):
    status = project.get("Status")
    if status:
        return status

    # Inference fallback based on Deployment Date
    deployment_date = project.get("Deployment Date")
    created_time = project.get("Created Time")

    if deployment_date:
        from datetime import datetime
        today = datetime.utcnow().date()

        try:
            dep_date = datetime.strptime(deployment_date[:10], "%Y-%m-%d").date()
            if dep_date < today:
                return "✅ Deployed (inferred)"
            else:
                return "⌛ In Progress (inferred)"
        except:
            return "❓ Unknown"

    if created_time:
        return "🚫 Not Started (inferred)"

    return "❓ Unknown"

def findMatches(value,target):
    import re
    from difflib import get_close_matches

    # Stop words to ignore in tokenization
    stop_words = {
        "when", "did", "we", "deploy", "deployed", "the", "to", "of", "on", "in", "for",
        "hi", "hello", "hey", "there", "how", "are", "you", "what", "is", "this",
        "project", "about", "can", "tell", "me", "more"
    }

    # Normalize and tokenize
    def tokenize(text):
        words = re.findall(r'\w+', text.lower())
        return set(word for word in words if word not in stop_words)

    query = target
    project_name = value

    if not project_name or not query:
        return False

    query_keywords = tokenize(query)
    project_keywords = tokenize(project_name)

    # Step 1: Keyword overlap
    overlap = len(query_keywords & project_keywords)
    if overlap > 0:
        return True

    # Step 2: Fallback fuzzy match
    normalized_query = ''.join(query_keywords)
    normalized_project = ''.join(project_keywords)
    matches = get_close_matches(normalized_query, [normalized_project], n=1, cutoff=0.8)

    return bool(matches)

def format_multiple_projects_flash_message(projects, parsed_query):
    if not projects:
        return "No matching projects found."

    def format_field(label, value, suffix=""):
        return f"• {label}: {value}{suffix}" if value else None

    # Map parsed_query keys to display labels and optional suffixes
    field_display_map = {
        "created_time": ("Created Time", ""),
        "original_due_date": ("Original Due Date", ""),
        "deployment_date": ("Deployment Date", ""),
        "total_hours": ("Total Project Hours", " hrs"),
        "dev_hours": ("Projected Dev Hours", " hrs"),
        "qi_hours": ("Projected QI Hours", " hrs"),
        "status": ("Status", ""),
        "project_name": ("Project Name", ""),
    }

    # Convert camel_case parsed_query keys to the display-friendly fields we care about
    requested_fields = {field_display_map[k][0]: field_display_map[k][1]
                        for k in parsed_query.keys() if k in field_display_map}
    print(f"Requested fields: {requested_fields}")
    messages = []
    for project in projects:
        lines = [f"- :pushpin: *`{project.get('Project Name', 'Unnamed Project')}`*"]

        if "Status" in requested_fields and project.get("Status"):
            lines.append(f"Status: {project['Status']}")

        for label, suffix in requested_fields.items():
            if label == "Status":  # already handled above
                continue
            value = project.get(label)
            field_line = format_field(label, value, suffix)
            if field_line:
                lines.append(field_line)

        messages.append("\n".join(lines))

    return "\n\n".join(messages)

