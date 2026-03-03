// Background service worker for Ridea Chrome Extension
// Handles extension lifecycle events

chrome.runtime.onInstalled.addListener(() => {
  console.log('Ridea extension installed');
});

// Keep service worker alive if needed
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === 'ping') {
    sendResponse({ type: 'pong' });
  }
  return true;
});
