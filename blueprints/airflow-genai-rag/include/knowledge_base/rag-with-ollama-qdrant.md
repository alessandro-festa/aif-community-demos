# RAG with Ollama and Qdrant

Retrieval-Augmented Generation (RAG) grounds a large language model's answers in
your own data. Instead of relying only on what the model learned during training,
a RAG pipeline retrieves relevant passages from a knowledge base at query time and
includes them in the prompt, so the model can cite current, domain-specific facts.

A RAG pipeline has two phases. In the ingestion phase you split source documents
into chunks, compute an embedding vector for each chunk, and store those vectors in
a vector database. In the retrieval phase you embed the user's query with the same
embedding model, run a nearest-neighbor search against the stored vectors, and pass
the top matches to the language model as context.

Ollama runs open language models locally and exposes a simple HTTP API. The
`/api/embed` endpoint returns embeddings — this blueprint uses the
`nomic-embed-text` model for that — while `/api/generate` and `/api/chat` produce
text. Because Ollama runs on CPU here, it favors small models such as
`llama3.2:1b`, which keep latency reasonable without a GPU.

Qdrant is a lightweight, open-source vector database. A collection stores vectors
alongside a payload of scalar metadata such as the chunk text, its title, and its
source document. Collections are schemaless for the payload — you only declare the
vector size and distance metric (COSINE works well for text embeddings) when
creating one, and the payload fields can be anything. Qdrant exposes a simple
RESTful API, so clients can create collections, upsert points, and search using
plain HTTP.

Choosing the embedding model matters: the same model must be used for both
ingestion and querying, because vectors are only comparable within the same model's
space. If you change the embedding model, re-ingest the whole knowledge base so the
stored vectors and the query vectors line up.
