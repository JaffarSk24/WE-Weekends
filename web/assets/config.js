// API address. A local copy of the page talks to a local gateway.
export const API = ['localhost', '127.0.0.1'].includes(location.hostname)
  ? 'http://localhost:8790'
  : 'https://weekends-api.whiteeagles.sk';
