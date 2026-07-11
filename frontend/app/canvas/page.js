import { redirect } from 'next/navigation';

// A canvas is now always a specific Workshop (see app/canvas/[workshopId]/
// page.js) — there's no more single default board to land on here.
export default function CanvasIndexPage() {
  redirect('/projects');
}
