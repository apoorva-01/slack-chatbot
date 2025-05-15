import os
import re
import faiss
import pickle
import json
import numpy as np
import google.generativeai as genai
import google.api_core.exceptions  # Add this import
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from concurrent.futures import ThreadPoolExecutor
from googleapiclient.errors import HttpError
from difflib import get_close_matches
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
# def get_google_docs_content(doc_id):
#     """Fetches text content from a Google Doc."""
#     creds = service_account.Credentials.from_service_account_info(
#         GOOGLE_CREDENTIALS, scopes=["https://www.googleapis.com/auth/documents.readonly"]
#     )
#     service = build("docs", "v1", credentials=creds)
#     doc = service.documents().get(documentId=doc_id).execute()
#     content = ""
#     for element in doc.get("body", {}).get("content", []):
#         if "paragraph"in element:
#             for text_run in element["paragraph"].get("elements", []):
#                 if "textRun"in text_run:
#                     content += text_run["textRun"]["content"] + ""
#     return content.strip()

def get_google_docs_content(doc_id, retries=5, delay=2):
    """Fetches text content from a Google Doc with retry on rate limits."""
    creds = service_account.Credentials.from_service_account_info(
        GOOGLE_CREDENTIALS, scopes=["https://www.googleapis.com/auth/documents.readonly"]
    )
    service = build("docs", "v1", credentials=creds)

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

    content = ""
    for element in doc.get("body", {}).get("content", []):
        if "paragraph" in element:
            for text_run in element["paragraph"].get("elements", []):
                if "textRun" in text_run:
                    content += text_run["textRun"]["content"]
    return content.strip()

# 📌 2️⃣ Text Chunking for Better Retrieval
# def chunk_text(text, max_size=5000, overlap=2000):
#     """Splits text into overlapping chunks while maintaining sentence boundaries."""
#     sentences = re.split(r'(?<=[.!?])\s+', text)  # Split by sentence
#     chunks = []
#     current_chunk = []
#     current_size = 0
#     for sentence in sentences:
#         if current_size + len(sentence) > max_size and current_chunk:
#             chunks.append("".join(current_chunk))  # Store chunk
#             overlap_count = max(1, overlap // max(1, len(current_chunk)))  # Avoid division by zero
#             current_chunk = current_chunk[-overlap_count:]
#             current_size = sum(len(s) for s in current_chunk)
#         current_chunk.append(sentence)
#         current_size += len(sentence)
#     if current_chunk:
#         chunks.append("".join(current_chunk))  # Add last chunk
#     return chunks

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


def extract_client_from_query(query, available_clients):
    """Extracts the closest matching client name from the query using fuzzy matching."""
    
    # Try exact match first
    for client in available_clients:
        if re.search(rf"\b{re.escape(client)}\b", query, re.IGNORECASE):
            return client  # Found an exact match

    # If no exact match, try partial match
    words_in_query = query.lower().split()
    for client in available_clients:
        client_words = client.lower().split()
        if any(word in words_in_query for word in client_words):  # Match any part
            return client

    # Fallback: Use fuzzy matching if no match is found
    closest_match = get_close_matches(query, available_clients, n=1, cutoff=0.5)
    return closest_match[0] if closest_match else None


def get_gemini_embedding_parallel(text_chunks):
    with ThreadPoolExecutor() as executor:
        results = list(executor.map(get_gemini_embedding, text_chunks))
    return results

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
        """Fetches, chunks, and indexes documents per client."""
        # Delete existing files in the index directory
        for file in os.listdir(self.index_dir):
            file_path = os.path.join(self.index_dir, file)
            if os.path.isfile(file_path):
                os.remove(file_path)
        print(f"🗑️ Cleared existing files in {self.index_dir}.")

        docs_by_client = {}

        # Fetch & organize documents by client
        for doc in documents:
            client = doc["clientName"]
            doc_id = doc["docId"]
            text = get_google_docs_content(doc_id)

            if not text.strip():
                print(f"⚠️ Empty document: {doc_id} ({client}) - Skipping.")
                continue
            if client not in docs_by_client:
                docs_by_client[client] = []
            docs_by_client[client].append(text)

        for doc in notion_documents:
            client = doc["clientName"]
            doc_id = doc["docId"]
            text = get_google_docs_content(doc_id)

            if not text.strip():
                print(f"⚠️ Empty notion document: {doc_id} ({client}) - Skipping.")
                continue

            # Chunk and embed notion documents
            chunks = chunk_text(text)
            embeddings = get_gemini_embedding_parallel(chunks)
            dimension = len(embeddings[0])

            # Create or load FAISS index for notion documents
            notion_index_path = os.path.join(self.index_dir, f"{client}.faiss")
            notion_docstore_path = os.path.join(self.index_dir, f"{client}_docstore.pkl")
            if os.path.exists(notion_index_path):
                index = faiss.read_index(notion_index_path)
                with open(notion_docstore_path, "rb") as f:
                    notion_docstore = pickle.load(f)
            else:
                index = faiss.IndexHNSWFlat(dimension, 32)
                notion_docstore = {}

            # Add embeddings and update the document store
            index.add(np.array(embeddings, dtype=np.float32))
            for i, chunk in enumerate(chunks):
                notion_docstore[len(notion_docstore)] = chunk

            # Save the updated index and document store
            faiss.write_index(index, notion_index_path)
            with open(notion_docstore_path, "wb") as f:
                pickle.dump(notion_docstore, f)
            print(f"💾 FAISS index created/updated for notion_documents of {client} with {len(chunks)} chunks.")

        # Add hubspot_documents to docs_by_client
        for doc in hubspot_documents:
            client = doc["clientName"]
            doc_id = doc["docId"]
            text = get_google_docs_content(doc_id)

            if not text.strip():
                print(f"⚠️ Empty hubspot document: {doc_id} ({client}) - Skipping.")
                continue

            # Chunk and embed hubspot documents
            chunks = chunk_text(text)
            embeddings = get_gemini_embedding_parallel(chunks)
            dimension = len(embeddings[0])

            # Create or load FAISS index for hubspot documents
            hubspot_index_path = os.path.join(self.index_dir, f"{client}_hubspot.faiss")
            hubspot_docstore_path = os.path.join(self.index_dir, f"{client}_hubspot_docstore.pkl")
            if os.path.exists(hubspot_index_path):
                index = faiss.read_index(hubspot_index_path)
                with open(hubspot_docstore_path, "rb") as f:
                    hubspot_docstore = pickle.load(f)
            else:
                index = faiss.IndexHNSWFlat(dimension, 32)
                hubspot_docstore = {}

            # Add embeddings and update the document store
            index.add(np.array(embeddings, dtype=np.float32))
            for i, chunk in enumerate(chunks):
                hubspot_docstore[len(hubspot_docstore)] = chunk

            # Save the updated index and document store
            faiss.write_index(index, hubspot_index_path)
            with open(hubspot_docstore_path, "wb") as f:
                pickle.dump(hubspot_docstore, f)
            print(f"💾 FAISS index created/updated for hubspot_documents of {client} with {len(chunks)} chunks.")

        # Add raw_messages_documents to docs_by_client
        for doc in raw_messages_documents:
            client = doc["clientName"]
            doc_id = doc["docId"]
            text = get_google_docs_content(doc_id)

            if not text.strip():
                print(f"⚠️ Empty raw messages document: {doc_id} ({client}) - Skipping.")
                continue

            # Chunk and embed raw messages documents
            chunks = chunk_text(text)
            embeddings = get_gemini_embedding_parallel(chunks)
            dimension = len(embeddings[0])

            # Create or load FAISS index for raw messages documents
            raw_messages_index_path = os.path.join(self.index_dir, f"{client}_raw_messages.faiss")
            raw_messages_docstore_path = os.path.join(self.index_dir, f"{client}_raw_messages_docstore.pkl")
            if os.path.exists(raw_messages_index_path):
                index = faiss.read_index(raw_messages_index_path)
                with open(raw_messages_docstore_path, "rb") as f:
                    raw_messages_docstore = pickle.load(f)
            else:
                index = faiss.IndexHNSWFlat(dimension, 32)
                raw_messages_docstore = {}

            # Add embeddings and update the document store
            index.add(np.array(embeddings, dtype=np.float32))
            for i, chunk in enumerate(chunks):
                raw_messages_docstore[len(raw_messages_docstore)] = chunk

            # Save the updated index and document store
            faiss.write_index(index, raw_messages_index_path)
            with open(raw_messages_docstore_path, "wb") as f:
                pickle.dump(raw_messages_docstore, f)
            print(f"💾 FAISS index created/updated for raw_messages_documents of {client} with {len(chunks)} chunks.")

        # Add transcript_documents to docs_by_client
        for doc in transcript_documents:
            client = doc["clientName"]
            doc_id = doc["docId"]
            text = get_google_docs_content(doc_id)

            if not text.strip():
                print(f"⚠️ Empty transcript document: {doc_id} ({client}) - Skipping.")
                continue

            # Chunk and embed transcript documents
            chunks = chunk_text(text)
            embeddings = get_gemini_embedding_parallel(chunks)
            dimension = len(embeddings[0])

            # Create or load FAISS index for transcript documents
            transcript_index_path = os.path.join(self.index_dir, f"{client}_transcript.faiss")
            transcript_docstore_path = os.path.join(self.index_dir, f"{client}_transcript_docstore.pkl")
            if os.path.exists(transcript_index_path):
                index = faiss.read_index(transcript_index_path)
                with open(transcript_docstore_path, "rb") as f:
                    transcript_docstore = pickle.load(f)
            else:
                index = faiss.IndexHNSWFlat(dimension, 32)
                transcript_docstore = {}

            # Add embeddings and update the document store
            index.add(np.array(embeddings, dtype=np.float32))
            for i, chunk in enumerate(chunks):
                transcript_docstore[len(transcript_docstore)] = chunk

            # Save the updated index and document store
            faiss.write_index(index, transcript_index_path)
            with open(transcript_docstore_path, "wb") as f:
                pickle.dump(transcript_docstore, f)
            print(f"💾 FAISS index created/updated for transcript_documents of {client} with {len(chunks)} chunks.")

        # Add faq_documents to docs_by_client
        for doc in faq_documents:
            client = doc["clientName"]
            doc_id = doc["docId"]
            text = get_google_docs_content(doc_id)

            if not text.strip():
                print(f"⚠️ Empty FAQ document: {doc_id} ({client}) - Skipping.")
                continue

            # Chunk and embed FAQ documents
            chunks = chunk_text(text)
            embeddings = get_gemini_embedding_parallel(chunks)
            dimension = len(embeddings[0])

            # Create or load FAISS index for FAQ documents
            faq_index_path = os.path.join(self.index_dir, f"{client}_faq.faiss")
            faq_docstore_path = os.path.join(self.index_dir, f"{client}_faq_docstore.pkl")
            if os.path.exists(faq_index_path):
                index = faiss.read_index(faq_index_path)
                with open(faq_docstore_path, "rb") as f:
                    faq_docstore = pickle.load(f)
            else:
                index = faiss.IndexHNSWFlat(dimension, 32)
                faq_docstore = {}

            # Add embeddings and update the document store
            index.add(np.array(embeddings, dtype=np.float32))
            for i, chunk in enumerate(chunks):
                faq_docstore[len(faq_docstore)] = chunk

            # Save the updated index and document store
            faiss.write_index(index, faq_index_path)
            with open(faq_docstore_path, "wb") as f:
                pickle.dump(faq_docstore, f)
            print(f"💾 FAISS index created/updated for faq_documents of {client} with {len(chunks)} chunks.")

        # Add internal_slack_messages_documents to docs_by_client
        for doc in internal_slack_messages_documents:
            client = doc["clientName"]
            doc_id = doc["docId"]
            text = get_google_docs_content(doc_id)

            if not text.strip():
                print(f"⚠️ Empty internal Slack messages document: {doc_id} ({client}) - Skipping.")
                continue

            # Chunk and embed internal Slack messages documents
            chunks = chunk_text(text)
            embeddings = get_gemini_embedding_parallel(chunks)
            dimension = len(embeddings[0])

            # Create or load FAISS index for internal Slack messages documents
            slack_index_path = os.path.join(self.index_dir, f"{client}_slack.faiss")
            slack_docstore_path = os.path.join(self.index_dir, f"{client}_slack_docstore.pkl")
            if os.path.exists(slack_index_path):
                index = faiss.read_index(slack_index_path)
                with open(slack_docstore_path, "rb") as f:
                    slack_docstore = pickle.load(f)
            else:
                index = faiss.IndexHNSWFlat(dimension, 32)
                slack_docstore = {}

            # Add embeddings and update the document store
            index.add(np.array(embeddings, dtype=np.float32))
            for i, chunk in enumerate(chunks):
                slack_docstore[len(slack_docstore)] = chunk

            # Save the updated index and document store
            faiss.write_index(index, slack_index_path)
            with open(slack_docstore_path, "wb") as f:
                pickle.dump(slack_docstore, f)
            print(f"💾 FAISS index created/updated for internal_slack_messages_documents of {client} with {len(chunks)} chunks.")

        # Process each client's documents
        for client, docs in docs_by_client.items():
            print(f"📌 Indexing {len(docs)} documents for {client}...")

            all_chunks = []
            chunk_map = {}  # Maps chunk index to actual chunk text

            for doc_id, doc in enumerate(docs):
                chunks = chunk_text(doc)  # Split document into smaller chunks
                for chunk_id, chunk in enumerate(chunks):
                    all_chunks.append(chunk)
                    chunk_map[len(all_chunks) - 1] = chunk  # Store chunk ID → text

            # Generate embeddings
            embeddings = get_gemini_embedding_parallel(all_chunks)
            dimension = len(embeddings[0])

            # Create FAISS index
            index = faiss.IndexHNSWFlat(dimension, 32)
            index.add(np.array(embeddings, dtype=np.float32))

            # Store index and chunk mapping
            self.indexes[client] = index
            self.docstores[client] = chunk_map  # Store chunk mapping
            print(f"✅ FAISS index created for {client} with {len(all_chunks)} chunks.")
            self.save_index(client)


    def get_notion_chunks(self, client_name):
        """Retrieve all chunks created using notion_documents for a specific client."""
        notion_index_path = os.path.join(self.index_dir, f"{client_name}_notion.faiss")
        if not os.path.exists(notion_index_path):
            print(f"⚠️ No FAISS index found for notion_documents of {client_name}.")
            return []

        # Load the FAISS index for notion_documents
        index = faiss.read_index(notion_index_path)

        # Retrieve all chunks from the notion document store
        notion_docstore_path = os.path.join(self.index_dir, f"{client_name}_notion_docstore.pkl")
        if os.path.exists(notion_docstore_path):
            with open(notion_docstore_path, "rb") as f:
                notion_docstore = pickle.load(f)
            notion_chunks = list(notion_docstore.values())
            # print(f"✅ Retrieved {len(notion_chunks)} chunks from notion_documents for {client_name}.")
            return notion_chunks
        else:
            print(f"⚠️ No document store found for notion_documents of {client_name}.")
            return []

    def get_chunks(self, client_name):
        """Retrieve all chunks created using notion_documents for a specific client."""
        notion_index_path = os.path.join(self.index_dir, f"{client_name}.faiss")
        if not os.path.exists(notion_index_path):
            print(f"⚠️ No FAISS index found for notion_documents of {client_name}.")
            return []

        # Load the FAISS index for notion_documents
        index = faiss.read_index(notion_index_path)

        # Retrieve all chunks from the notion document store
        notion_docstore_path = os.path.join(self.index_dir, f"{client_name}_docstore.pkl")
        if os.path.exists(notion_docstore_path):
            with open(notion_docstore_path, "rb") as f:
                notion_docstore = pickle.load(f)
            notion_chunks = list(notion_docstore.values())
            # print(f"✅ Retrieved {len(notion_chunks)} chunks from notion_documents for {client_name}.")
            return notion_chunks
        else:
            print(f"⚠️ No document store found for notion_documents of {client_name}.")
            return []

    def get_hubspot_chunks(self, client_name):
        """Retrieve all chunks created using hubspot_documents for a specific client."""
        hubspot_index_path = os.path.join(self.index_dir, f"{client_name}_hubspot.faiss")
        if not os.path.exists(hubspot_index_path):
            print(f"⚠️ No FAISS index found for hubspot_documents of {client_name}.")
            return []

        # Load the FAISS index for hubspot_documents
        hubspot_docstore_path = os.path.join(self.index_dir, f"{client_name}_hubspot_docstore.pkl")
        if os.path.exists(hubspot_docstore_path):
            with open(hubspot_docstore_path, "rb") as f:
                hubspot_docstore = pickle.load(f)
            return list(hubspot_docstore.values())
        else:
            print(f"⚠️ No document store found for hubspot_documents of {client_name}.")
            return []

    def get_raw_messages_chunks(self, client_name):
        """Retrieve all chunks created using raw_messages_documents for a specific client."""
        raw_messages_index_path = os.path.join(self.index_dir, f"{client_name}_raw_messages.faiss")
        if not os.path.exists(raw_messages_index_path):
            print(f"⚠️ No FAISS index found for raw_messages_documents of {client_name}.")
            return []

        # Load the FAISS index for raw_messages_documents
        raw_messages_docstore_path = os.path.join(self.index_dir, f"{client_name}_raw_messages_docstore.pkl")
        if os.path.exists(raw_messages_docstore_path):
            with open(raw_messages_docstore_path, "rb") as f:
                raw_messages_docstore = pickle.load(f)
            return list(raw_messages_docstore.values())
        else:
            print(f"⚠️ No document store found for raw_messages_documents of {client_name}.")
            return []

    def get_transcript_chunks(self, client_name):
        """Retrieve all chunks created using transcript_documents for a specific client."""
        transcript_index_path = os.path.join(self.index_dir, f"{client_name}_transcript.faiss")
        if not os.path.exists(transcript_index_path):
            print(f"⚠️ No FAISS index found for transcript_documents of {client_name}.")
            return []

        # Load the FAISS index for transcript_documents
        transcript_docstore_path = os.path.join(self.index_dir, f"{client_name}_transcript_docstore.pkl")
        if os.path.exists(transcript_docstore_path):
            with open(transcript_docstore_path, "rb") as f:
                transcript_docstore = pickle.load(f)
            return list(transcript_docstore.values())
        else:
            print(f"⚠️ No document store found for transcript_documents of {client_name}.")
            return []

    def get_faq_chunks(self, client_name):
        """Retrieve all chunks created using faq_documents for a specific client."""
        faq_index_path = os.path.join(self.index_dir, f"{client_name}_faq.faiss")
        if not os.path.exists(faq_index_path):
            print(f"⚠️ No FAISS index found for faq_documents of {client_name}.")
            return []

        # Load the FAISS index for faq_documents
        faq_docstore_path = os.path.join(self.index_dir, f"{client_name}_faq_docstore.pkl")
        if os.path.exists(faq_docstore_path):
            with open(faq_docstore_path, "rb") as f:
                faq_docstore = pickle.load(f)
            return list(faq_docstore.values())
        else:
            print(f"⚠️ No document store found for faq_documents of {client_name}.")
            return []

    def get_internal_slack_chunks(self, client_name):
        """Retrieve all chunks created using internal_slack_messages_documents for a specific client."""
        slack_index_path = os.path.join(self.index_dir, f"{client_name}_slack.faiss")
        if not os.path.exists(slack_index_path):
            print(f"⚠️ No FAISS index found for internal_slack_messages_documents of {client_name}.")
            return []

        # Load the FAISS index for internal_slack_messages_documents
        slack_docstore_path = os.path.join(self.index_dir, f"{client_name}_slack_docstore.pkl")
        if os.path.exists(slack_docstore_path):
            with open(slack_docstore_path, "rb") as f:
                slack_docstore = pickle.load(f)
            return list(slack_docstore.values())
        else:
            print(f"⚠️ No document store found for internal_slack_messages_documents of {client_name}.")
            return []
        

    def search_faiss(self, query, client_name, top_k=5):
        print(f"🔍 Searching FAISS for query: '{query}' in client: {client_name}...")
        """Search FAISS for relevant document chunks, including all document types."""
        # Load FAISS index for the matched client
        self.load_index(client_name)
        notion_chunks = self.get_chunks(client_name)

        # Generate query embedding
        query_embedding = get_gemini_embedding(query)

        # Perform FAISS search for hubspot_chunks
        hubspot_chunks = []
        hubspot_index_path = os.path.join(self.index_dir, f"{client_name}_hubspot.faiss")
        if os.path.exists(hubspot_index_path):
            hubspot_index = faiss.read_index(hubspot_index_path)
            D, I = hubspot_index.search(np.array([query_embedding]), top_k)
            hubspot_docstore_path = os.path.join(self.index_dir, f"{client_name}_hubspot_docstore.pkl")
            if os.path.exists(hubspot_docstore_path):
                with open(hubspot_docstore_path, "rb") as f:
                    hubspot_docstore = pickle.load(f)
                hubspot_chunks = [hubspot_docstore.get(i, None) for i in I[0] if i != -1]

        # Perform FAISS search for raw_messages_chunks
        raw_messages_chunks = []
        raw_messages_index_path = os.path.join(self.index_dir, f"{client_name}_raw_messages.faiss")
        if os.path.exists(raw_messages_index_path):
            raw_messages_index = faiss.read_index(raw_messages_index_path)
            D, I = raw_messages_index.search(np.array([query_embedding]), top_k)
            raw_messages_docstore_path = os.path.join(self.index_dir, f"{client_name}_raw_messages_docstore.pkl")
            if os.path.exists(raw_messages_docstore_path):
                with open(raw_messages_docstore_path, "rb") as f:
                    raw_messages_docstore = pickle.load(f)
                raw_messages_chunks = [raw_messages_docstore.get(i, None) for i in I[0] if i != -1]

        # Perform FAISS search for transcript_chunks
        transcript_chunks = []
        transcript_index_path = os.path.join(self.index_dir, f"{client_name}_transcript.faiss")
        if os.path.exists(transcript_index_path):
            transcript_index = faiss.read_index(transcript_index_path)
            D, I = transcript_index.search(np.array([query_embedding]), top_k)
            transcript_docstore_path = os.path.join(self.index_dir, f"{client_name}_transcript_docstore.pkl")
            if os.path.exists(transcript_docstore_path):
                with open(transcript_docstore_path, "rb") as f:
                    transcript_docstore = pickle.load(f)
                transcript_chunks = [transcript_docstore.get(i, None) for i in I[0] if i != -1]

        # Perform FAISS search for faq_chunks
        faq_chunks = []
        faq_index_path = os.path.join(self.index_dir, f"{client_name}_faq.faiss")
        if os.path.exists(faq_index_path):
            faq_index = faiss.read_index(faq_index_path)
            D, I = faq_index.search(np.array([query_embedding]), top_k)
            faq_docstore_path = os.path.join(self.index_dir, f"{client_name}_faq_docstore.pkl")
            if os.path.exists(faq_docstore_path):
                with open(faq_docstore_path, "rb") as f:
                    faq_docstore = pickle.load(f)
                faq_chunks = [faq_docstore.get(i, None) for i in I[0] if i != -1]

        # Perform FAISS search for internal_slack_messages_chunks
        slack_chunks = []
        slack_index_path = os.path.join(self.index_dir, f"{client_name}_slack.faiss")
        if os.path.exists(slack_index_path):
            slack_index = faiss.read_index(slack_index_path)
            D, I = slack_index.search(np.array([query_embedding]), top_k)
            slack_docstore_path = os.path.join(self.index_dir, f"{client_name}_slack_docstore.pkl")
            if os.path.exists(slack_docstore_path):
                with open(slack_docstore_path, "rb") as f:
                    slack_docstore = pickle.load(f)
                slack_chunks = [slack_docstore.get(i, None) for i in I[0] if i != -1]

        # Combine all chunks and return separately
        return {
            "notion_chunks": notion_chunks,
            "hubspot_chunks": [chunk for chunk in hubspot_chunks if chunk],
            "raw_messages_chunks": [chunk for chunk in raw_messages_chunks if chunk],
            "transcript_chunks": [chunk for chunk in transcript_chunks if chunk],
            "faq_chunks": [chunk for chunk in faq_chunks if chunk],
            "slack_chunks": [chunk for chunk in slack_chunks if chunk],
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