import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";

/*
 * Everything is behind sign-in except the landing page.
 *
 * The matcher is an allowlist of public routes rather than a blocklist of
 * private ones, which is the difference between forgetting to protect a new
 * page and forgetting to expose one. The first is a disclosure; the second is
 * a bug report.
 */

const isPublic = createRouteMatcher(["/", "/sign-in(.*)", "/sign-up(.*)"]);

export default clerkMiddleware(async (auth, request) => {
  if (!isPublic(request)) {
    await auth.protect();
  }
});

export const config = {
  matcher: [
    // Everything except Next internals and static files, plus all API routes.
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    "/(api|trpc)(.*)",
  ],
};
