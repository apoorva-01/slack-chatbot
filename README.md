# Slack Bot with AI-Powered Assistance

## Overview
This project is a Slack bot designed to provide intelligent, context-aware assistance by leveraging advanced AI technologies. It integrates with OpenAI's ChatGPT, Google's Gemini, and FAISS (Facebook AI Similarity Search) to deliver accurate and relevant responses to user queries. The bot is capable of retrieving client-specific information, generating responses, and interacting with users in Slack channels.

---

## Features

### 1. Slack Integration
- Listens for mentions in Slack channels and processes user queries in real-time.
- Supports interactive feedback, allowing users to regenerate responses or provide feedback.

### 2. AI-Powered Responses
- Generates intelligent responses using OpenAI's ChatGPT and Google's Gemini models.
- Compares responses from both models to ensure accuracy and selects the best one.

### 3. Document Retrieval
- Uses FAISS to retrieve relevant chunks of information from a pre-indexed set of client-specific documents.
- Ensures responses are accurate and contextually relevant.

### 4. Thread Context Awareness
- Fetches conversation history in Slack threads to provide context-aware responses.

### 5. Asynchronous Processing
- Handles tasks like document retrieval and response generation efficiently using asynchronous programming.

---

## How It Works

1. **User Query**:
   - A user mentions the bot in a Slack channel with a query.
   
2. **Client Identification**:
   - The bot identifies the relevant client or assistant based on the query.

3. **Document Retrieval**:
   - Retrieves relevant document chunks using FAISS.

4. **Response Generation**:
   - Generates responses using ChatGPT and Gemini, compares them, and selects the best response.

5. **Feedback Mechanism**:
   - Users can provide feedback or request a regenerated response.

---

## Technical Details

### 1. Backend
- Built using **Python** and **Flask** for handling Slack events and API requests.

### 2. AI Models
- **OpenAI's ChatGPT**: Used for generating conversational responses.
- **Google's Gemini**: Used for generating embeddings and responses.

### 3. Document Retrieval
- **FAISS**: Used for vector-based similarity search to retrieve relevant document chunks.

### 4. Slack Integration
- **Slack SDK**: Used for interacting with Slack's API to send and receive messages.

### 5. Asynchronous Processing
- **Asyncio**: Used for handling tasks like FAISS searches and response generation in parallel.

---

## Installation

### Prerequisites
- Python 3.8 or higher
- Slack workspace and bot token
- OpenAI API key
- Google Gemini API key
- FAISS library installed

### Steps
1. Clone the repository:
   ```bash
   git clone https://github.com/your-repo/slackbot.git
   cd slackbot
   ```

2. Create a virtual environment and activate it:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Set up environment variables in a `.env` file:

5. Run the application:
   ```bash
   python main.py
   ```

---

## Usage

1. Mention the bot in a Slack channel with your query.
2. The bot will identify the relevant client, retrieve information, and generate a response.
3. Provide feedback or request a regenerated response using interactive buttons.

---

## File Structure

```
slackbot/
├── faiss_index/               # Directory for FAISS indexes and document stores
├── .vscode/                   # VSCode settings
├── .env                       # Environment variables
├── main.py                    # Main application file
├── index_client_data.py       # Handles FAISS indexing and document retrieval
├── README.md                  # Project documentation
├── requirements.txt           # Python dependencies
```

---

## Demo

1. **Query Example**:
   - User: "@bot What is the latest sales report for Barton Watches?"
   - Bot: "Here is the latest sales report for Barton Watches: [retrieved data]."

2. **Feedback Example**:
   - User clicks "Regenerate" to request a new response.
   - Bot regenerates the response and sends it back.

---

## Future Enhancements

1. Add support for more AI models.
2. Improve client identification using advanced NLP techniques.
3. Enhance the feedback mechanism for better user interaction.

---

## Contributing

Contributions are welcome! Please fork the repository and submit a pull request for any improvements or bug fixes.

---

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.
