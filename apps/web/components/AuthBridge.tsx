"use client";

import { useAuth } from "@clerk/nextjs";
import { useEffect } from "react";
import { setTokenSource } from "@/lib/api";

/**
 * Connects Clerk's session to the API client.
 *
 * Mounted once, above everything that makes a request. `getToken` is stable
 * across renders and refreshes the token itself when it is close to expiring,
 * so registering it once is enough — the client asks for a token per request
 * and always gets a live one.
 *
 * The cleanup matters: on sign-out the source is cleared, so a request that
 * somehow escapes the signed-out UI goes out with no credential and is refused,
 * rather than replaying the last token this tab happened to hold.
 */
export function AuthBridge({ children }: { children: React.ReactNode }) {
  const { getToken, isSignedIn } = useAuth();

  useEffect(() => {
    if (!isSignedIn) {
      setTokenSource(null);
      return;
    }
    setTokenSource(() => getToken());
    return () => setTokenSource(null);
  }, [getToken, isSignedIn]);

  return <>{children}</>;
}
