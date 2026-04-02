export default function handler(req, res) {
  res.json({
    googleClientId: process.env.GOOGLE_CLIENT_ID || '',
  });
}
