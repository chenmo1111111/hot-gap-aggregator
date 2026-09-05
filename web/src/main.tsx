import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import './styles.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode><App /></StrictMode>,
);

if ('serviceWorker' in navigator && import.meta.env.PROD) {
  window.addEventListener('load', () => {
    const hadController = Boolean(navigator.serviceWorker.controller);
    void navigator.serviceWorker.register('./sw.js', { updateViaCache: 'none' }).then((registration) => {
      void registration.update();
      window.setInterval(() => void registration.update(), 60 * 60 * 1000);
    });
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (hadController) window.location.reload();
    }, { once: true });
  });
}
