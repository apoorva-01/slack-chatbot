import os
import re
import faiss
import pickle
import json
import numpy as np
import google.generativeai as genai
import google.api_core.exceptions
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from concurrent.futures import ThreadPoolExecutor
from googleapiclient.errors import HttpError
from difflib import get_close_matches
from document_ids import (
    documents,
    notion_documents,
    hubspot_documents,
    raw_messages_documents,
    transcript_documents,
    faq_documents,
    internal_slack_messages_documents,
)
load_dotenv()
import time
import random

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_CREDENTIALS = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
genai.configure(api_key=GEMINI_API_KEY)


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
                # print(f"🔃 FAISS index loaded for {client_name}")
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

    def process_and_index_documents(self, documents, client_suffix, index_suffix, docstore_suffix):
        """Generic method to process and index documents for a given client and document type."""
        for doc in documents:
            client = doc["clientName"]
            doc_id = doc["docId"]
            text = get_google_docs_content(doc_id)

            if not text.strip():
                print(f"⚠️ Empty {client_suffix} document: {doc_id} ({client}) - Skipping.")
                continue

            # Chunk and embed documents
            chunks = chunk_text(text)
            embeddings = get_gemini_embedding_parallel(chunks)
            dimension = len(embeddings[0])

            # Create or load FAISS index
            index_path = os.path.join(self.index_dir, f"{client}{index_suffix}.faiss")
            docstore_path = os.path.join(self.index_dir, f"{client}{docstore_suffix}")
            if os.path.exists(index_path):
                index = faiss.read_index(index_path)
                with open(docstore_path, "rb") as f:
                    docstore = pickle.load(f)
            else:
                index = faiss.IndexHNSWFlat(dimension, 32)
                docstore = {}

            # Add embeddings and update the document store
            index.add(np.array(embeddings, dtype=np.float32))
            for i, chunk in enumerate(chunks):
                docstore[len(docstore)] = chunk

            # Save the updated index and document store
            faiss.write_index(index, index_path)
            with open(docstore_path, "wb") as f:
                pickle.dump(docstore, f)
            print(f"💾 FAISS index created/updated for {client_suffix} documents of {client} with {len(chunks)} chunks.")

    def create_index(self, documents):
        """Fetches, chunks, and indexes documents per client."""
        # Step 1: Clear existing files in the index directory
        for file in os.listdir(self.index_dir):
            file_path = os.path.join(self.index_dir, file)
            if os.path.isfile(file_path):
                os.remove(file_path)
        print(f"🗑️ Cleared existing files in {self.index_dir}.")

        # Process and index all document types
        self.process_and_index_documents(notion_documents, "notion", "", "_docstore.pkl")
        self.process_and_index_documents(hubspot_documents, "hubspot", "_hubspot", "_hubspot_docstore.pkl")
        self.process_and_index_documents(raw_messages_documents, "raw_messages", "_raw_messages", "_raw_messages_docstore.pkl")
        self.process_and_index_documents(transcript_documents, "transcript", "_transcript", "_transcript_docstore.pkl")
        self.process_and_index_documents(faq_documents, "faq", "_faq", "_faq_docstore.pkl")
        self.process_and_index_documents(internal_slack_messages_documents, "internal_slack", "_slack", "_slack_docstore.pkl")

        # Process each client's documents
        docs_by_client = {}
        for doc in documents:
            client = doc["clientName"]
            text = get_google_docs_content(doc["docId"])
            if not text.strip():
                print(f"⚠️ Empty document: {doc['docId']} ({client}) - Skipping.")
                continue
            docs_by_client.setdefault(client, []).append(text)

        for client, docs in docs_by_client.items():
            print(f"📌 Indexing {len(docs)} documents for {client}...")

            all_chunks = []
            chunk_map = {}  # Maps chunk index to actual chunk text

            for doc in docs:
                chunks = chunk_text(doc)  # Split document into smaller chunks
                for chunk in chunks:
                    all_chunks.append(chunk)
                    chunk_map[len(all_chunks) - 1] = {
                        "text": chunk,
                        "docId": doc.get("docId", ""),
                        "type": doc.get("type", ""),
                        "client": client,
                    }

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

    def get_chunks_by_type(self, client_name, source_type, index_suffix="", docstore_suffix="_docstore.pkl"):
        """Generic method to retrieve all chunks for a given client and source type."""
        index_filename = f"{client_name}{index_suffix}.faiss"
        index_path = os.path.join(self.index_dir, index_filename)

        if not os.path.exists(index_path):
            print(f"⚠️ No FAISS index found for {source_type} of {client_name}.")
            return []

        # Load the FAISS index (even if unused, assuming required for logic consistency)
        faiss.read_index(index_path)

        docstore_filename = f"{client_name}{docstore_suffix}"
        docstore_path = os.path.join(self.index_dir, docstore_filename)

        if os.path.exists(docstore_path):
            with open(docstore_path, "rb") as f:
                docstore = pickle.load(f)
            return list(docstore.values())
        else:
            print(f"⚠️ No document store found for {source_type} of {client_name}.")
            return []
    def get_chunks(self, client_name):
        return self.get_chunks_by_type(client_name, source_type="notion_documents")

    def get_hubspot_chunks(self, client_name):
        return self.get_chunks_by_type(client_name, source_type="hubspot_documents", index_suffix="_hubspot", docstore_suffix="_hubspot_docstore.pkl")

    def get_raw_messages_chunks(self, client_name):
        return self.get_chunks_by_type(client_name, source_type="raw_messages_documents", index_suffix="_raw_messages", docstore_suffix="_raw_messages_docstore.pkl")

    def get_transcript_chunks(self, client_name):
        return self.get_chunks_by_type(client_name, source_type="transcript_documents", index_suffix="_transcript", docstore_suffix="_transcript_docstore.pkl")

    def get_faq_chunks(self, client_name):
        return self.get_chunks_by_type(client_name, source_type="faq_documents", index_suffix="_faq", docstore_suffix="_faq_docstore.pkl")

    def get_internal_slack_chunks(self, client_name):
        return self.get_chunks_by_type(client_name, source_type="internal_slack_messages_documents", index_suffix="_slack", docstore_suffix="_slack_docstore.pkl")

    def search_faiss(self, query, client_name, top_k=20):
        print(f"🔍 Searching FAISS for query: '{query}' in client: {client_name}...")
        
        # Load index and Notion chunks
        self.load_index(client_name)
        notion_chunks = self.get_chunks(client_name)

        # Generate query embedding
        query_embedding = get_gemini_embedding(query)

        def search_document_type(doc_type):
            chunks = []
            index_path = os.path.join(self.index_dir, f"{client_name}_{doc_type}.faiss")
            docstore_path = os.path.join(self.index_dir, f"{client_name}_{doc_type}_docstore.pkl")

            if os.path.exists(index_path):
                index = faiss.read_index(index_path)
                D, I = index.search(np.array([query_embedding]), top_k)
                if os.path.exists(docstore_path):
                    with open(docstore_path, "rb") as f:
                        docstore = pickle.load(f)
                    chunks = [docstore.get(i, None) for i in I[0] if i != -1]
            return [chunk for chunk in chunks if chunk]

        return {
            "notion_chunks": notion_chunks,
            "hubspot_chunks": search_document_type("hubspot"),
            "raw_messages_chunks": search_document_type("raw_messages"),
            "transcript_chunks": search_document_type("transcript"),
            "faq_chunks": search_document_type("faq"),
            "slack_chunks": search_document_type("slack"),
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
