import './globals.css';

export const metadata = {
  title: 'AI Discovery Canvas',
  description: 'AI Discovery Canvas — engagement intelligence',
};

// Runs before first paint so a saved dark preference never flashes light.
// Kept as a plain inline script (not a component/effect — those run after
// hydration, far too late). suppressHydrationWarning on <html> because the
// server can't know data-theme.
const themeInit = `(function(){try{
  var t = localStorage.getItem('aidc-theme');
  if (t !== 'dark' && t !== 'light') {
    t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
  }
  document.documentElement.setAttribute('data-theme', t);
}catch(e){}})();`;

export default function RootLayout({ children }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <script dangerouslySetInnerHTML={{ __html: themeInit }} />
        {children}
      </body>
    </html>
  );
}
