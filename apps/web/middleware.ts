import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";

/*
 * Everything is behind sign-in except the landing page.
 *
 * The matcher is an allowlist of public routes rather than a blocklist of
 * private ones, which is the difference between forgetting to protect a new
 * page and forgetting to expose one. The first is a disclosure; the second is
 * a bug report.
 */

// /privacy is public deliberately: someone deciding whether to hand this
// system their marksheet needs to read what happens to it *before* signing up,
// and putting that page behind sign-in would be its own answer to the question.
const isPublic = createRouteMatcher([
  "/",
  "/privacy",
  "/sign-in(.*)",
  "/sign-up(.*)",
]);

export default clerkMiddleware(async (auth, request) => {
  if (!isPublic(request)) {
    await auth.protect();
  }
});

export const config = {
  matcher: [
    // Everything except Next internals, static files and /privacy, plus all
    // API routes.
    //
    // /privacy is excluded from the matcher rather than only allow-listed
    // above. Allow-listing still runs the middleware, which in development
    // performs a handshake redirect to Clerk before rendering — so the one
    // page a reader must be able to open *before* deciding to trust this
    // system depended on a third party being reachable. It needs no auth
    // context at all, so it should not ask for one.
    "/((?!_next|privacy|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    "/(api|trpc)(.*)",
  ],
};
