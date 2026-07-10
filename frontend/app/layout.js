import './globals.css';

// No design system, no custom fonts — just the system font stack (see
// globals.css). UI polish is explicitly out of scope for this scaffold;
// the priority is a working backbone (see root README).
export const metadata = {
  title: 'AI Discovery Canvas',
  description: 'AI Discovery Canvas — backbone scaffold',
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
