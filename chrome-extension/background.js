// Background service worker for Idea Capture Chrome Extension
// Handles extension lifecycle events

chrome.runtime.onInstalled.addListener(() => {
  console.log('Idea Capture extension installed');
});

// Keep service worker alive if needed
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === 'ping') {
    sendResponse({ type: 'pong' });
  }
  return true;
});
