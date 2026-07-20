# RAG with Ollama and Milvus

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

Milvus is an open-source vector database. A collection stores vectors alongside
scalar metadata such as the chunk text, its title, and its source document. An
index (AUTOINDEX with a COSINE metric works well for text embeddings) makes
similarity search fast. Milvus exposes a RESTful v2 API through its proxy, so
clients can create collections, insert entities, and search using plain HTTP.

Choosing the embedding model matters: the same model must be used for both
ingestion and querying, because vectors are only comparable within the same model's
space. If you change the embedding model, re-ingest the whole knowledge base so the
stored vectors and the query vectors line up.
