import os
import re
import faiss
import pickle
import json
import numpy as np
from datetime import datetime, timezone
import google.generativeai as genai
import google.api_core.exceptions
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from concurrent.futures import ThreadPoolExecutor
from googleapiclient.errors import HttpError
from google_auth_httplib2 import AuthorizedHttp
import httplib2
load_dotenv()
import time
import random
from document_ids import (
    documents,
    notion_documents,
    hubspot_documents,
    raw_messages_documents,
    transcript_documents,
    faq_documents,
    internal_slack_messages_documents,
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_CREDENTIALS =  json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
# Ensure Google Gemini API is properly configured
genai.configure(api_key=GEMINI_API_KEY)

# 📌 1️⃣ Fetch Google Docs Content
def get_google_docs_content(doc_id, retries=5, delay=2, timeout=120):
    """Fetches text content from a Google Doc with retry on rate limits and configurable timeout."""
    creds = service_account.Credentials.from_service_account_info(
        GOOGLE_CREDENTIALS, scopes=["https://www.googleapis.com/auth/documents.readonly"]
    )
    
    # Create an HTTP object with a custom timeout
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=timeout))
    service = build("docs", "v1", http=http)

    for attempt in range(retries):
        try:
            doc = service.documents().get(documentId=doc_id).execute()
            break
        except HttpError as e:
            if e.resp.status in [403, 429, 500, 503] and attempt < retries - 1:
                wait = delay * (2 ** attempt) + random.uniform(0, 1)
                print(f"⏳ Rate limit or server error. Retrying in {wait:.2f} seconds...")
                time.sleep(wait)
            else:
                raise
        except TimeoutError as e:
            if attempt < retries - 1:
                wait = delay * (2 ** attempt) + random.uniform(0, 1)
                print(f"⏳ Timeout occurred. Retrying in {wait:.2f} seconds...")
                time.sleep(wait)
            else:
                print("❌ Failed to fetch document due to repeated timeouts.")
                raise
        except httplib2.ServerNotFoundError as e:
            if attempt < retries - 1:
                wait = delay * (2 ** attempt) + random.uniform(0, 1)
                print(f"⏳ Server not found. Retrying in {wait:.2f} seconds...")
                time.sleep(wait)
            else:
                print("❌ Failed to fetch document due to server not found error.")
                raise

    content = ""
    for element in doc.get("body", {}).get("content", []):
        if "paragraph" in element:
            for text_run in element["paragraph"].get("elements", []):
                if "textRun" in text_run:
                    content += text_run["textRun"]["content"]
    return content.strip()

# 📌 2️⃣ Text Chunking for Better Retrieval
def chunk_text(text, max_size=5000, overlap=2000):
    """Splits text into smaller chunks (e.g., 500 characters) for better retrieval."""
    return [text[i:i + max_size] for i in range(0, len(text), max_size)]

# 📌 3️⃣ Get Embeddings from Gemini
def get_gemini_embedding(text, retries=5, delay=2):
    """Generate embeddings for text using Google Gemini, handling large texts and rate limits."""
    model = "models/embedding-001"
    text_chunks = chunk_text(text)
    embeddings = []

    for chunk in text_chunks:
        for attempt in range(retries):
            try:
                response = genai.embed_content(model=model, content=chunk, task_type="retrieval_document")
                embeddings.append(response["embedding"])
                break  # Exit retry loop on success
            except google.api_core.exceptions.ResourceExhausted as e:
                if attempt < retries - 1:
                    wait = delay * (2 ** attempt) + random.uniform(0, 1)
                    print(f"⏳ Quota exceeded. Retrying in {wait:.2f} seconds...")
                    time.sleep(wait)
                else:
                    print(f"❌ Failed to embed content after {retries} attempts due to quota limits.")
                    raise
            except Exception as e:
                print(f"⚠️ Unexpected error: {e}")
                raise

    # Average embeddings if multiple chunks
    avg_embedding = [sum(col) / len(col) for col in zip(*embeddings)]
    return avg_embedding

def get_gemini_embedding_parallel(text_chunks):
    with ThreadPoolExecutor() as executor:
        results = list(executor.map(get_gemini_embedding, text_chunks))
    return results




def parse_numeric_value(value):
    if value.strip().lower() in {"n/a", "na", "n\\a", "n\\/a"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None

def parse_date(value):
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None

def infer_status(project):
    status = project.get("Status")
    if status:
        return status

    deployment_date = project.get("Deployment Date")
    created_time = project.get("Created Time")

    if deployment_date:
        today = datetime.now(timezone.utc).date()
        try:
            dep_date = datetime.strptime(deployment_date[:10], "%Y-%m-%d").date()
            if dep_date < today:
                return "✅ Deployed (Not directly mentioned in data, I calculated using dates provided)"
            else:
                return "⌛ In Progress (Not directly mentioned in data, I calculated using dates provided)"
        except:
            return "❓ Unknown"

    if created_time:
        return "🚫 Not Started (Not directly mentioned in data, I calculated using dates provided)"

    return "❓ Unknown"

def finalize_comments(comment_lines):
    text = "\n".join(comment_lines).strip()
    if "no comments available" in text.lower() or not text:
        return None
    return text

def parse_project_data(doc_text):
    projects = []
    current_project = {}
    comment_lines = []
    in_comments_section = False

    field_starts = re.compile(
        r"^(Project Name|Status|Created Time|Original Due Date|Deployment Date|Total Project Hours|Projected Dev Hours|Projected QI Hours|Details):"
    )

    lines = doc_text.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        if line.startswith("Project Name:"):
            if current_project and current_project.get("Project Name"):
                current_project["Comments"] = finalize_comments(comment_lines)
                current_project["Status"] = infer_status(current_project)
                projects.append(current_project)
                current_project = {}
                comment_lines = []
                in_comments_section = False
            current_project["Project Name"] = line.split(":", 1)[1].strip()

        elif line.startswith("Status:"):
            value = line.split(":", 1)[1].strip()
            if value:
                current_project["Status"] = value

        elif line.startswith("Created Time:"):
            value = line.split(":", 1)[1].strip()
            dt = parse_date(value)
            current_project["Created Time"] = dt.isoformat() if dt else None

        elif line.startswith("Original Due Date:"):
            value = line.split(":", 1)[1].strip()
            dt = parse_date(value)
            current_project["Original Due Date"] = dt.isoformat() if dt else None

        elif line.startswith("Deployment Date:"):
            value = line.split(":", 1)[1].strip()
            dt = parse_date(value)
            current_project["Deployment Date"] = dt.isoformat() if dt else None

        elif line.startswith("Total Project Hours:"):
            value = line.split(":", 1)[1].strip()
            current_project["Total Project Hours"] = parse_numeric_value(value)

        elif line.startswith("Projected Dev Hours:"):
            value = line.split(":", 1)[1].strip()
            current_project["Projected Dev Hours"] = parse_numeric_value(value)

        elif line.startswith("Projected QI Hours:"):
            value = line.split(":", 1)[1].strip()
            current_project["Projected QI Hours"] = parse_numeric_value(value)

        elif line.startswith("Details:"):
            details_lines = [line.split(":", 1)[1].strip()]
            i += 1
            while i < len(lines):
                next_line = lines[i].strip()
                if field_starts.match(next_line) or "Comments:" in next_line:
                    i -= 1
                    break
                details_lines.append(next_line)
                i += 1
            details = "\n".join(details_lines).strip()
            current_project["Details"] = None if details == "---------------------------------------------------------------------" else details

        elif line.startswith("Task:"):
            current_project["Task"] = line.split(":", 1)[1].strip()
            if in_comments_section:
                comment_lines.append(line)

        elif "Comments:" in line:
            in_comments_section = True
            parts = line.split("Comments:", 1)
            if parts[1].strip():
                comment_lines.append(parts[1].strip())
            i += 1
            continue

        elif in_comments_section:
            if field_starts.match(line):
                in_comments_section = False
                i -= 1
                continue
            comment_lines.append(line)

        i += 1

    # Final project block
    if current_project.get("Project Name"):
        current_project["Comments"] = finalize_comments(comment_lines)
        if not current_project.get("Status"):
            current_project["Status"] = infer_status(current_project)
        projects.append(current_project)

    return projects

# 📌 4️⃣ FAISS Vector Store Class
class FAISSVectorStore:
    def __init__(self, index_dir="faiss_index"):
        self.indexes = {}
        self.docstores = {}
        self.index_dir = index_dir
        os.makedirs(self.index_dir, exist_ok=True)

    def load_index(self, client_name):
        """Load FAISS index and document store from disk."""
        index_path = os.path.join(self.index_dir, f"{client_name}.faiss")
        docstore_path = os.path.join(self.index_dir, f"{client_name}_docstore.pkl")
        if os.path.exists(index_path):
            self.indexes[client_name] = faiss.read_index(index_path)
            if os.path.exists(docstore_path):
                with open(docstore_path, "rb") as f:
                    self.docstores[client_name] = pickle.load(f)
                print(f"🔃 FAISS index loaded for {client_name}")
            else:
                print(f"⚠️ No document store found for {client_name}.")
        else:
            print(f"⚠️ No FAISS index found for {client_name}.")


    def save_index(self, client_name):
        """Save FAISS index and document store to disk."""
        if client_name not in self.indexes:
            print(f"⚠️ No FAISS index found for {client_name}. Skipping save.")
            return
        index_path = os.path.join(self.index_dir, f"{client_name}.faiss")
        docstore_path = os.path.join(self.index_dir, f"{client_name}_docstore.pkl")
        faiss.write_index(self.indexes[client_name], index_path)
        with open(docstore_path, "wb") as f:
            pickle.dump(self.docstores[client_name], f)
        print(f"💾 FAISS index saved for {client_name}")

    def create_index(self, documents):
        self._clear_index_dir()
        
        docs_by_client = self._organize_google_docs(documents)
        self._process_notion_documents(notion_documents)
        
        self._process_special_documents(hubspot_documents, "hubspot")
        self._process_special_documents(raw_messages_documents, "raw_messages")
        self._process_special_documents(transcript_documents, "transcript")
        self._process_special_documents(faq_documents, "faq")
        self._process_special_documents(internal_slack_messages_documents, "slack")
        
        self._process_client_documents(docs_by_client)


    def _clear_index_dir(self):
        for file in os.listdir(self.index_dir):
            file_path = os.path.join(self.index_dir, file)
            if os.path.isfile(file_path):
                os.remove(file_path)
        print(f"🗑️ Cleared existing files in {self.index_dir}.")


    def _organize_google_docs(self, documents):
        docs_by_client = {}
        for doc in documents:
            client = doc["clientName"]
            doc_id = doc["docId"]
            text = get_google_docs_content(doc_id)
            if not text.strip():
                print(f"⚠️ Empty document: {doc_id} ({client}) - Skipping.")
                continue
            docs_by_client.setdefault(client, []).append(text)
        return docs_by_client


    def _process_notion_documents(self, notion_documents):
        for doc in notion_documents:
            client = doc["clientName"]
            doc_id = doc["docId"]
            text = get_google_docs_content(doc_id)
            if not text.strip():
                print(f"⚠️ Empty notion document: {doc_id} ({client}) - Skipping.")
                continue
            project_data = parse_project_data(text)
            file_path = os.path.join("faiss_index", f"{client}_notion.json")
            # Save the list of dictionaries as a JSON array
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(project_data, f, indent=4)
            print(f"JSON data saved to {file_path}")


    def _process_special_documents(self, document_list, doc_type):
        for doc in document_list:
            client = doc["clientName"]
            doc_id = doc["docId"]
            text = get_google_docs_content(doc_id)
            if not text.strip():
                print(f"⚠️ Empty {doc_type} document: {doc_id} ({client}) - Skipping.")
                continue

            chunks = chunk_text(text)
            embeddings = get_gemini_embedding_parallel(chunks)
            dimension = len(embeddings[0])

            index_path = os.path.join(self.index_dir, f"{client}_{doc_type}.faiss")
            docstore_path = os.path.join(self.index_dir, f"{client}_{doc_type}_docstore.pkl")

            if os.path.exists(index_path):
                index = faiss.read_index(index_path)
                with open(docstore_path, "rb") as f:
                    docstore = pickle.load(f)
            else:
                index = faiss.IndexHNSWFlat(dimension, 32)
                docstore = {}

            index.add(np.array(embeddings, dtype=np.float32))
            for i, chunk in enumerate(chunks):
                docstore[len(docstore)] = chunk

            faiss.write_index(index, index_path)
            with open(docstore_path, "wb") as f:
                pickle.dump(docstore, f)
            print(f"💾 FAISS index created/updated for {doc_type} of {client} with {len(chunks)} chunks.")


    def _process_client_documents(self, docs_by_client):
        for client, docs in docs_by_client.items():
            print(f"📌 Indexing {len(docs)} documents for {client}...")
            all_chunks = []
            chunk_map = {}

            for doc in docs:
                chunks = chunk_text(doc)
                for chunk in chunks:
                    chunk_map[len(all_chunks)] = chunk
                    all_chunks.append(chunk)

            embeddings = get_gemini_embedding_parallel(all_chunks)
            dimension = len(embeddings[0])
            index = faiss.IndexHNSWFlat(dimension, 32)
            index.add(np.array(embeddings, dtype=np.float32))

            self.indexes[client] = index
            self.docstores[client] = chunk_map
            print(f"✅ FAISS index created for {client} with {len(all_chunks)} chunks.")
            self.save_index(client)

    def _load_notion_json(self, client_name, prefix):
        json_path = os.path.join(self.index_dir, f"{client_name}_{prefix}.json")
        if os.path.exists(json_path):
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)  # Load JSON as a single array
                return {"notion_chunks": data}  # Wrap it in a dictionary
        else:
            print(f"⚠️ No JSON file found for {prefix} of {client_name}.")
            return {"notion_chunks": []}  # Return an empty dictionary

    def _load_chunks(self, client_name, prefix):
        index_path = os.path.join(self.index_dir, f"{client_name}_{prefix}.faiss")
        docstore_path = os.path.join(self.index_dir, f"{client_name}_{prefix}_docstore.pkl")

        if not os.path.exists(index_path):
            print(f"⚠️ No FAISS index found for {prefix} of {client_name}.")
            return []

        if os.path.exists(docstore_path):
            with open(docstore_path, "rb") as f:
                docstore = pickle.load(f)
            return list(docstore.values())
        else:
            print(f"⚠️ No document store found for {prefix} of {client_name}.")
            return []

    def _faiss_search(self, client_name, prefix, query_embedding, top_k):
        index_path = os.path.join(self.index_dir, f"{client_name}_{prefix}.faiss")
        docstore_path = os.path.join(self.index_dir, f"{client_name}_{prefix}_docstore.pkl")

        if not os.path.exists(index_path):
            return []

        index = faiss.read_index(index_path)
        D, I = index.search(np.array([query_embedding]), top_k)

        if not os.path.exists(docstore_path):
            return []

        with open(docstore_path, "rb") as f:
            docstore = pickle.load(f)

        return [docstore.get(i) for i in I[0] if i != -1 and docstore.get(i)]

    def get_notion_chunks(self, client_name):
        return self._load_notion_json(client_name, "notion")

    def get_hubspot_chunks(self, client_name):
        return self._load_chunks(client_name, "hubspot")

    def get_raw_messages_chunks(self, client_name):
        return self._load_chunks(client_name, "raw_messages")

    def get_transcript_chunks(self, client_name):
        return self._load_chunks(client_name, "transcript")

    def get_faq_chunks(self, client_name):
        return self._load_chunks(client_name, "faq")

    def get_internal_slack_chunks(self, client_name):
        return self._load_chunks(client_name, "slack")

    def search_faiss(self, query, client_name, top_k=5):
        print(f"🔍 Searching FAISS for query: '{query}' in client: {client_name}...")

        query_embedding = get_gemini_embedding(query)

        notion_chunks = self.get_notion_chunks(client_name)
        if isinstance(notion_chunks, list):  # Handle list response
            notion_chunks = {"notion_chunks": notion_chunks}

        return {
            **notion_chunks,  # Merge notion_chunks dictionary
            "hubspot_chunks": self._faiss_search(client_name, "hubspot", query_embedding, top_k),
            "raw_messages_chunks": self._faiss_search(client_name, "raw_messages", query_embedding, top_k),
            "transcript_chunks": self._faiss_search(client_name, "transcript", query_embedding, top_k),
            "faq_chunks": self._faiss_search(client_name, "faq", query_embedding, top_k),
            "slack_chunks": self._faiss_search(client_name, "slack", query_embedding, top_k),
        }



if __name__ == "__main__":

    faiss_store = FAISSVectorStore()
    faiss_store.create_index(documents)

    # Search for relevant document parts
    # results = faiss_store.search_faiss("latest sales report","Barton Watches", top_k=5)
    # print("length of results",len(results))
    # for i, chunk in enumerate(results):
    #     print(f"\n🔹 Chunk {i+1}: {chunk[:200]}...")  # Print first 200 chars
    #     print("length of cunk",{i+1},len(chunk)