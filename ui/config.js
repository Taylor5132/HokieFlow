// The Python server handles accounts. Campus services remain team integrations.
export const config = Object.freeze({
  mode: 'backend', apiBase: '',
  homeEndpoint: '',
  // Must be a same-origin route that starts Google OAuth on the backend.
  googleAuthUrl: '/api/auth/google'
});
