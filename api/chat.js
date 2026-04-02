import { GoogleAuth } from 'google-auth-library';

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const { serviceAccountKey, projectId, location, model, messages, temperature, maxTokens } = req.body;

  if (!serviceAccountKey || !projectId || !messages?.length) {
    return res.status(400).json({ error: 'Missing serviceAccountKey, projectId, or messages' });
  }

  const loc = location || 'us-central1';

  const MODEL_IDS = {
    'gemini-2.5-pro': 'gemini-2.5-pro-preview-05-06',
    'gemini-2.5-flash': 'gemini-2.5-flash-preview-04-17',
  };

  const modelId = MODEL_IDS[model] || MODEL_IDS['gemini-2.5-flash'];

  try {
    // Parse the service account key
    let keyData;
    try {
      keyData = typeof serviceAccountKey === 'string'
        ? JSON.parse(serviceAccountKey)
        : serviceAccountKey;
    } catch (e) {
      return res.status(400).json({ error: 'Invalid service account JSON key' });
    }

    // Create auth client from service account key
    const auth = new GoogleAuth({
      credentials: keyData,
      scopes: ['https://www.googleapis.com/auth/cloud-platform'],
    });

    const client = await auth.getClient();
    const accessToken = (await client.getAccessToken()).token;

    // Call Vertex AI streaming endpoint
    const url = `https://${loc}-aiplatform.googleapis.com/v1/projects/${projectId}/locations/${loc}/publishers/google/models/${modelId}:streamGenerateContent`;

    const body = {
      contents: messages,
      generationConfig: {
        temperature: temperature ?? 1.0,
        maxOutputTokens: maxTokens ?? 8192,
      },
    };

    const apiRes = await fetch(url, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${accessToken}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    });

    if (!apiRes.ok) {
      const err = await apiRes.text();
      let errMsg;
      try { errMsg = JSON.parse(err).error?.message || err; } catch { errMsg = err; }
      return res.status(apiRes.status).json({ error: errMsg });
    }

    // Stream the response back as SSE
    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('Connection', 'keep-alive');

    const reader = apiRes.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // Vertex AI streams JSON array chunks: [{...},\n{...},\n...]
      // Parse complete JSON objects from the buffer
      const lines = buffer.split('\n');
      buffer = lines.pop(); // keep incomplete line in buffer

      for (const line of lines) {
        const trimmed = line.trim().replace(/^\[?\,?/, '').replace(/\]$/, '').trim();
        if (!trimmed) continue;

        try {
          const chunk = JSON.parse(trimmed);
          res.write(`data: ${JSON.stringify(chunk)}\n\n`);
        } catch {
          // not a complete JSON object yet, put it back
        }
      }
    }

    // Process remaining buffer
    if (buffer.trim()) {
      const trimmed = buffer.trim().replace(/^\[?\,?/, '').replace(/\]$/, '').trim();
      if (trimmed) {
        try {
          const chunk = JSON.parse(trimmed);
          res.write(`data: ${JSON.stringify(chunk)}\n\n`);
        } catch { /* ignore */ }
      }
    }

    res.write('data: [DONE]\n\n');
    res.end();
  } catch (e) {
    return res.status(500).json({ error: e.message });
  }
}
