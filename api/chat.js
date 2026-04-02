import { GoogleAuth } from 'google-auth-library';

export const config = {
  maxDuration: 60,
};

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
    'gemini-2.5-pro': 'gemini-2.5-pro',
    'gemini-2.5-flash': 'gemini-2.5-flash',
  };

  const modelId = MODEL_IDS[model] || MODEL_IDS['gemini-2.5-flash'];

  try {
    let keyData;
    try {
      keyData = typeof serviceAccountKey === 'string'
        ? JSON.parse(serviceAccountKey)
        : serviceAccountKey;
    } catch (e) {
      return res.status(400).json({ error: 'Invalid service account JSON key' });
    }

    // Get access token from service account
    const auth = new GoogleAuth({
      credentials: keyData,
      scopes: ['https://www.googleapis.com/auth/cloud-platform'],
    });

    const client = await auth.getClient();
    const accessToken = (await client.getAccessToken()).token;

    // Call Vertex AI (non-streaming to avoid Vercel stream issues, then forward as SSE)
    const url = `https://${loc}-aiplatform.googleapis.com/v1/projects/${projectId}/locations/${loc}/publishers/google/models/${modelId}:generateContent`;

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

    const responseText = await apiRes.text();

    if (!apiRes.ok) {
      let errMsg;
      try { errMsg = JSON.parse(responseText).error?.message || responseText; } catch { errMsg = responseText; }
      return res.status(apiRes.status).json({ error: errMsg });
    }

    // Parse the full response and send as SSE chunks
    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('Connection', 'keep-alive');

    const data = JSON.parse(responseText);

    // Vertex AI returns either a single object or an array
    const chunks = Array.isArray(data) ? data : [data];
    for (const chunk of chunks) {
      res.write(`data: ${JSON.stringify(chunk)}\n\n`);
    }

    res.write('data: [DONE]\n\n');
    res.end();
  } catch (e) {
    console.error('Chat API error:', e);
    if (!res.headersSent) {
      return res.status(500).json({ error: e.message });
    }
    res.end();
  }
}
