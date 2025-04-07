import os
import re
import faiss
import pickle
import json
import numpy as np
import google.generativeai as genai
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from concurrent.futures import ThreadPoolExecutor
from difflib import get_close_matches
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_CREDENTIALS =  json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
# Ensure Google Gemini API is properly configured
genai.configure(api_key=GEMINI_API_KEY)

# 📌 1️⃣ Fetch Google Docs Content
def get_google_docs_content(doc_id):
    """Fetches text content from a Google Doc."""
    creds = service_account.Credentials.from_service_account_info(
        GOOGLE_CREDENTIALS, scopes=["https://www.googleapis.com/auth/documents.readonly"]
    )
    service = build("docs", "v1", credentials=creds)
    doc = service.documents().get(documentId=doc_id).execute()
    content = ""
    for element in doc.get("body", {}).get("content", []):
        if "paragraph"in element:
            for text_run in element["paragraph"].get("elements", []):
                if "textRun"in text_run:
                    content += text_run["textRun"]["content"] + ""
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
def get_gemini_embedding(text):
    """Generate embeddings for text using Google Gemini, handling large texts."""
    model = "models/embedding-001"
    text_chunks = chunk_text(text)
    embeddings = []
    for chunk in text_chunks:
        response = genai.embed_content(model=model, content=chunk, task_type="retrieval_document")
        embeddings.append(response["embedding"])  
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

        # Add notion_documents to docs_by_client
        notion_documents = [
            {"clientName": "voluspa", "docId": "1KjyTZk1suvDlarlLSmNXQbE2JhlabDGhMPs7gFay_Kg"},
            {"clientName": "mymedic", "docId": "1XPuZqwMITL2khUXHqC8XrI45X-GtwctSO5Am8N2H-Bc"},
            {"clientName": "manlyman", "docId": "1uxmBY7zcpErNXAB0jamP1LEARsr3pzAOYwRjMggOYXg"},
            {"clientName": "jdsports", "docId": "1UIxZXgoqP8CRo6-_7sUVwjk4-BbgODKAbxPcoXqM8zc"},
            {"clientName": "malbongolf", "docId": "1eK3yaoC4dXPm8A7hdoPpsbBfjpY4vxcfKdmzJaPwlls"},
            {"clientName": "marijuana-packaging", "docId": "1johA13MY_0Kk-bHc8DHQ5XwyVx7LfInJRRl5STlh_W8"},
            {"clientName": "taosfootwear", "docId": "1ru8qf73Ok7cTB375drFy5lDWP4Eqd-qkNqm6DYPkwgg"},
            {"clientName": "shepherdsfashion", "docId": "10oSs_ngN0m6O4mrHcYU4vY6r5KLOBuCA5oXaObn6gPY"},
            {"clientName": "goldenlions", "docId": "18mewpwTQEE9PWKrZAXl_x5haX1KikK8dIf8L31LlsW4"},
            {"clientName": "fullglasswineco", "docId": "1iAQjRGAR5535WvQFfZPCe49jzVpubbgOzueeXoZfPcE"},
            {"clientName": "duck-camp", "docId": "1juTAGXE3SaESDCaxdvzGTDtCDRGjJfEgciCGvkLp4WA"},
            {"clientName": "emazing-group", "docId": "1PpajHpvp5cSjlEFQ8xJLRkjfeBZuQ7XIJG4SyJtvySM"},
            {"clientName": "silverfernbrand", "docId": "1i5jdZBi5DUEIJwWgTBB__o5_kGtp4baTXCG5H5AoZL4"},
            {"clientName": "darntough", "docId": "101d6uAMLpki3sOsiC-IkUewgBsXmZDCmBRP5OlqpTaQ"},
            {"clientName": "bartonwatches", "docId": "1TwrTnEAqQ3JLvfQ3InOQBmhei7gUzTLNDyyBQMtnIpI"},
            {"clientName": "hammitt", "docId": "10tHo4Wsb-zVTh_8ZbAqwKi1ljXjlaDvzEtmEYakw_1g"},
            {"clientName": "dermdude", "docId": "1U3sJmr7lFW5YN_x27AMs5NFNPQ4TNZCj-I2brpqhI6s"},
            {"clientName": "inprintwetrustt", "docId": "1LZj61i2ZRUB_bgC2AVzP7nsvuTxCMeATeQPwsw-oYDM"},
            {"clientName": "florida-premium-beef", "docId": "18cVAc75DQJHhR3CRLBJEoJivJ6jiVFoXfnvyxisxaYw"},
            {"clientName": "warroad", "docId": "1_9W9QmfvEV9mQFIPTty3NYGIkzfTZzxubym2ao969ww"},
            {"clientName": "tailwind-nutrition", "docId": "1PkvKj7oUaNjFtDkIhJ0GLtzzr3it3pprAJdUmluJCjU"},
            {"clientName": "printfresh", "docId": "1tXIT5gxiNpzBJB0har-mbJEKGJUcvYaxPThkZvjDao0"},
            {"clientName": "mindbodygreen", "docId": "1opRxaXGvWyke2IqLRpaWxOcqDMhmIMyigwu5p0O_hWo"},
            {"clientName": "she-birdie", "docId": "1JEDy8KxcnI1BEMgR3fhY2wNpjKHNgUwu5U1wqUIdGNQ"},
            {"clientName": "axeandsledge", "docId": "1TKP5N_vcHmxvksuKJBX9MikZqsjfsd5zm4Zw3KdcaxY"},
            {"clientName": "heybud", "docId": "16oKaoAeq_-PzLDBjJzomnFV6qdWvC6c4jH8rm7aiP8I"},
            {"clientName": "jones-road", "docId": "1B6JO3SVwM-NlM2dMKh0ksXYLy4d5hIX5fPc4jEBhUis"},
            {"clientName": "createroom", "docId": "1LUuu5bQ8l6hY2sB2h3-TpXW-NUqAtsbcfC90tUgXGqo"},
            {"clientName": "choczero", "docId": "1x8aa0s3KLlEliSHz1j_POiuzHvaIUlMmGZmtYrX-nCQ"},
        ]
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
            notion_index_path = os.path.join(self.index_dir, f"{client}_notion.faiss")
            notion_docstore_path = os.path.join(self.index_dir, f"{client}_notion_docstore.pkl")
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

    def search_faiss(self, query, client_name, top_k=5):
        """Search FAISS for relevant document chunks, including chunks from notion_documents if applicable."""
        # Load FAISS index for the matched client
        self.load_index(client_name)
        notion_chunks = self.get_notion_chunks(client_name)

        if client_name not in self.indexes:
            print(f"❌ FAISS index not loaded for {client_name}.")

        # Generate query embedding
        query_embedding = get_gemini_embedding(query)

        # Perform FAISS search
        index = self.indexes[client_name]
        D, I = index.search(np.array([query_embedding]), top_k)

        # Retrieve matched document chunks
        relevant_chunks = [
            self.docstores[client_name].get(i, None) for i in I[0] if i != -1
        ]

        print(f"✅ Retrieved {len(relevant_chunks)} relevant chunks from FAISS for {client_name}.")
        # Combine FAISS chunks and notion chunks
        return [chunk for chunk in relevant_chunks if chunk] + notion_chunks

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
            print(f"✅ Retrieved {len(notion_chunks)} chunks from notion_documents for {client_name}.")
            return notion_chunks
        else:
            print(f"⚠️ No document store found for notion_documents of {client_name}.")
            return []


if __name__ == "__main__":
    documents = [
        {"clientName": "voluspa", "docId": "11mGn_sRZXGT5IGvbglefIxwSva2z6NYRJM0uS7pakhI"},
        {"clientName": "voluspa", "docId": "1HqbkZymWgIM0i9H2y6g6v6nadxqugwBNURe9Zm8ZODk"},
        {"clientName": "voluspa", "docId": "1o3o0DT5tmj0hkGtQtqVVP0Hb5YfhB1qY8zSWdstXJFc"},
        {"clientName": "voluspa", "docId": "1xzYlbD0aolnZ14ZZ-BF7Z6Xb5TYEXaoiFkHk93hht5U"},
        {"clientName": "voluspa", "docId": "1SzzRtWHsA4qqQ6Q-N7Puh-DsvsXPjFDRWhtS8u8PLsY"},
        {"clientName": "mymedic", "docId": "1qPB4oF_SpQDBRN4fnH7RqM5A5O4B-sHC3KVb1_B7cMs"},
        {"clientName": "mymedic", "docId": "18qU1wIc-zgUjMGZHZBikBHfC9gPj61LdqpTY352JE7c"},
        {"clientName": "mymedic", "docId": "1f48r_woNlO56dkZXyH56RGOWQp-HChALMyVZSqYvd9w"},
        {"clientName": "mymedic", "docId": "1t4tJdU-NsvnTFkQJFKZRBwPAaI_hNsbVArSNStIq2dk"},
        {"clientName": "jdsports", "docId": "1UIxZXgoqP8CRo6-_7sUVwjk4-BbgODKAbxPcoXqM8zc"},
        {"clientName": "jdsports", "docId": "1n1ST0x9xt81DJ5XhV026LGoMC0QUB8-u4YJztyOZA-M"},
        {"clientName": "marijuana-packaging", "docId": "1oHrxFSulVtYmcG2b0oV6T6H6jX-gKiKHfafhQ5Mx6rA"},
        {"clientName": "shepherdsfashion", "docId": "1d-wlYmqXis6dAoS8fFLco8JKN00pS0JI5duEI0-17EM"},
        {"clientName": "taosfootwear", "docId": "1ru8qf73Ok7cTB375drFy5lDWP4Eqd-qkNqm6DYPkwgg"},
        {"clientName": "taosfootwear", "docId": "1M073iw-iwGHUV13ofkTeHTZ3v8jf59mc5_QTThabaYc"},
        {"clientName": "manlyman", "docId": "16XpVRDlT8I0grBzyv1oVAHXX5R6ahX-NLj8XSfUhG8g"},
        {"clientName": "manlyman", "docId": "16XpVRDlT8I0grBzyv1oVAHXX5R6ahX-NLj8XSfUhG8g"},
        {"clientName": "malbongolf", "docId": "1A-oPQEtFIkACdX4f96FkutvYtU118jx6Js76xFc69eA"},
        {"clientName": "fullglasswineco", "docId": "19FucCr9tmydBxP9wQSZzbiKstCBBVyRXsvfEUlOgpM0"},
        {"clientName": "duck-camp", "docId": "1XNzVS9-biKS-CeWdENCnkl6n4OEDoVGlqhcQzzbnnvQ"},
        {"clientName": "emazing-group", "docId": "19EalaZPLEz5-pEx8wg68ygGc9menjOt3Pz0TnJGDr_M"},
        {"clientName": "silverfernbrand", "docId": "1p7eqFiZZ4IEmNUx3dXtP9mgIqZU0a04l4PLnE5X9atM"},
        {"clientName": "darntough", "docId": "11gZKIJUJCJ5A2hs-0Q86k0_4Wpt0klPC-ZXnQ-szny0"},
        {"clientName": "bartonwatches", "docId": "1q3OkpiQmPhsktGT4d7gi1zSdpNK4_FK3g7rCTkqlBCE"},
        {"clientName": "bartonwatches", "docId": "1cBTmy_XLkYQybcafxyIHqg69fQCIBjeHhqncCHdfmCc"},
        {"clientName": "hammitt", "docId": "1s_RPeOtX0RFVG7itF0dAx5x4i68yusFOAyBmH2FeAvk"},
        {"clientName": "hammitt", "docId": "1xomungD8HjhplvznguVE2hb3tX5NDdU9Y62ohEK8v9k"},
        {"clientName": "hammitt", "docId": "19etcTKinNNK92FoaW8YO68cEoj_lhXT74EyUYt8vcrQ"},
        {"clientName": "javier", "docId": "1ZB5HlvQPLWJjnTAqF7B0wqcsdNOexwtFU_HwEKtiVzI"},
        {"clientName": "javier", "docId": "1zi53vwSlXV4kaIkEk2TbcAjeolTrQJfD25o2iDam7eQ"},
        {"clientName": "javier", "docId": "1wbolZI-4j3J4K38bfSOqd-7FcMYo2KYp1S8a2L-0m6A"},
        {"clientName": "dermdude", "docId": "17_qW2bdjKo11Gf7UL53xtrxdRMNlvQBAHJEFD9b5JXE"},
        {"clientName": "dermdude", "docId": "1tm_VMWGVYjN2HeGgyoy0e2sglXmwRXW9QMvlxiCQCDY"},
        {"clientName": "dermdude", "docId": "1ho9_UhYHQEWy6cdgOFi-WObiOtZcVTVzIKlAhN9dKKI"},
        {"clientName": "goldenlions", "docId": "1AS6y6klZ9bpNGqfyfe4jYhGmG_vriDWd-2nioITSm0Q"},
        {"clientName": "goldenlions", "docId": "1Vk3kt4_g-SRNEF8I5EpRdRGsxwnNTi_nDuYsyM21Dvw"},
        {"clientName": "inprintwetrustt", "docId": "1_b6v78ONtKupGYD5e8yZUHYAxyjPwQzCoL1kRco46yU"},
        {"clientName": "florida-premium-beef", "docId": "1JkIRDsqm_--sp-phaHea2M7uJ2HJMf-KGWhJGianS8c"},
        {"clientName": "warroad", "docId": "1g5xYCuJ6SCL8P-CGd1428RFqrpxTAiLaLyWj4geLcjM"},
        {"clientName": "tailwind-nutrition", "docId": "1eeItg3vkKvC8eFVUr8gR8t55Y32GAdjFYbfQyCZQ83A"},
        {"clientName": "printfresh", "docId": "1WpEXv_IjonnrB7SzzQkiB3ZtGpAzOmKODHqfjOxqSZM"},
        {"clientName": "mindbodygreen", "docId": "1ibPebFFQ_8PRjwbDecXISNP3XbL8jkxvb23XwNzQ47c"},
        {"clientName": "shes-birdie", "docId": "1uyFBN-B-sKWVOe0T68JQ5YPMZb9vnewhHYXBbWLoGBU"},
        {"clientName": "axeandsledge", "docId": "1sqPYcPnQMRM53sJ84GkmEnkFpGpef3va9GKX7kkaJHY"}, 
        {"clientName": "heybud", "docId": "15nIRwwTStnzH1UvvRXWK9GZCxX4_iUihEksKw3RnMBk"},
        {"clientName": "jones-road", "docId": "1_NZFQA7A76VyX9mjdISXkztdIS2WAsygOkcGPKmdQ70"},
        {"clientName": "ivycityco", "docId": "1IFVUaQdbyHCIsrsJnDP4DRNvzm6upGSMOOu7LXcqheA"},
        {"clientName": "ivycityco", "docId": "1o8BTpVPxg32IOHhdJ-RCMndx8p-PUgNN9LOdfmp159k"},
        {"clientName": "ivycityco", "docId": "1cOJleDc1SF2nlmQS6hPVUn6lIXrRnZ4KTX6ygtrn-2o"},
        {"clientName": "createroom", "docId": "1OBNLVmPObPbOmr2SaOgYIRbrIvyf0Iv2-DAe2DbsvXs"},
        {"clientName": "choczero", "docId": "1XXwxTdiqJ61mKdRDc-797iO1xu2dyGrlQyvLRgxaDa8"},


        # {"clientName": "ChefIQ", "docId": "10jyj3MSCLVrkXx9Lr6uwwAv-wV5hkDRzODOgqJ1-goU"},
        # {"clientName": "Seeking Health", "docId": "1d4ilnj3Sz66l7eJp7-792kFsNjtx56nXakt-Gsu6dkU"},
        # {"clientName": "Seeking Health", "docId": "1uEDVWG5Bxnil1LlDhEzX9CU_hCQUdd_2m4hkpn7b1bQ"},

    ]

    faiss_store = FAISSVectorStore()
    # Index Client Data
    faiss_store.create_index(documents)

    # Search for relevant document parts
    # results = faiss_store.search_faiss("latest sales report","Barton Watches", top_k=5)
    # print("length of results",len(results))
    # for i, chunk in enumerate(results):
    #     print(f"\n🔹 Chunk {i+1}: {chunk[:200]}...")  # Print first 200 chars
    #     print("length of cunk",{i+1},len(chunk)
