import cv2

cap = cv2.VideoCapture("rtsp://10.180.145.41:8554/live")

while True:
    ret, frame = cap.read()
    if not ret:
        break

    cv2.imshow("Suometry", frame)

    if cv2.waitKey(1) == 27:
        break