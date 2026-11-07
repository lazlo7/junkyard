# Requirements

## Overview

Simple HTTP file server with curl as a client.

## Idea

This is a personal project, suited for my needs and demands, not something that is supposed to be advertised as a universal solution.  
Hence it may come forth as non-standard, hacky or quirky.

This is an simple HTTP file server. The file management (upload/modify/delete) is done via curl in the shell.  
The idea is to provide a way to share files from bare-bones headless environments using standard universally available tools (curl).  
There should be some sort of authentication via a password that the admin can remember and provide.  
Upload/modify/delete should only be accessible to admin. File read is accessible to everyone.
The communication with the file server should be made as simple as possible. I'm talking about literally having to type as few keystrokes as possible in the shell
to upload/modify/delete the files.

## Fuctional requirements

FR1: HTTP file server with typical CRUD semantics.

FR2: User receives back a short link for the file after upload (alongside other useful information if needed).
Potential short link format: example.com/<6+ uniformly random base64 characters>.

FR3: User provides the short link/id to modify/delete the file.

FR4: Authentication for upload/modify/delete operations.
These operations should be protected by a password (headless password).
It doesn't rotate/expire automatically. The user should remember it.
There should be a way to provide the password from a file, so that the password doesn't leak into the shell history.
Potential upload example:
```bash
curl -H'secretkey:' -T ./file https://example.com
# Here, the authentication is done by passing a custom header.
# The key itself serves as a secret, there is no value.
# This is sort of a "security by obscurity" challenge, as no one really expects
# authentication done via a secret header key, which is acceptable for this hacky project.
# Moreover, the headers are secured via HTTPS.
# Furthermore, this is a rather laconic way of passing authenticaion information.
# However, the possibility of passing custom headers via a file should be explored.
# Additionally, custom headers may be dropped by proxies, so this is only a potential upload example due for validation.
```

FR5: Secure links. User can request secure links to be returned for file upload.
Potential secure link format: example.com/<32 uniformly random base64 characters>

FR6: Expiration. By default, links expire in 30 days since creation.  
When the link expires, the respective file is deleted.  
Links can be made to never expire.  
Expiration parameter can be specified in the upload/modify request.

FR7: Regular files can be viewed in the browser.  
Things like text (plain viewing), images or videos (via HTML5 video), etc.
What can be reasonably viewed in the browser - should be.
Headless clients like curl/wget should just download the file, not download the HTML.

FR8: Rate-limiting. Even short links can lead to sensitive data and we want to limit potential brute-force/scanning.
Therefore some sort of rate-limiting should be implemented.  
I envision enforcing the policy of having not more than 1 request from one unathenticated IP in 1 second.

FR9: Some very simple frontend should be made for viewing the files.
The page should show the file (or say it can't be viewed and must be downloaded if it can't be viewed).  
The page should also include the id of the file, original filename (if possible), time of the upload/last modify, expiration info,
download button, share button.
Custom error/rate limiting page or structured messages (for headless clients) can be made.

FR10: Some simple admin frontend panel.  
It should be properly secured (proper authentication with cookie storage).  
It should display currently stored files list, storage info, provide a way to upload/modify/delete the file from the browser.
The admin panel should provide a way to change the headless password.

## Non-functional requirements

NFR1: curl serves as a client for the server.  
Authenticated and simple (fewest keystrokes) curl requests should be the priority.
Standartization/verbosity is not the target - be hacky to provide simplicity, while staying reasonably secure.
You can even invent a cryptic format that can be used to pass various parameters, 
as long as it can be reasonably typed out by a knowledgable admin and saves keystrokes.

NFR2: Arbitrary file formats should be supported (text, images, videos, etc.)

NFR3: Assume restricted, headless and minimal Linux environments being clients that can interact with the server.
For them, only the standard tools are assumed to available, including curl.
