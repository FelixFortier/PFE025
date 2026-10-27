function [tvec1,tvec2,tvec3,rmtx1,rmtx2,rmtx3] = LocateCameras(s1,s2,s3,squareSize)
    % Define images to process
    imageFileNames = {s1,s2,s3};
    
    % Detect calibration pattern in images
    detector = vision.calibration.monocular.CheckerboardDetector();
    [imagePoints, imagesUsed] = detectPatternPoints(detector, imageFileNames, 'HighDistortion', true);
    imageFileNames = imageFileNames(imagesUsed);
    
    % Read the first image to obtain image size
    originalImage = imread(imageFileNames{1});
    [mrows, ncols, ~] = size(originalImage);
    
    % Generate world coordinates for the planar pattern keypoints
    worldPoints = generateWorldPoints(detector, 'SquareSize', squareSize);
    
    % Calibrate the camera
    [cameraParams, imagesUsed, estimationErrors] = estimateCameraParameters(imagePoints, worldPoints, ...
        'EstimateSkew', false, 'EstimateTangentialDistortion', false, ...
        'NumRadialDistortionCoefficients', 3, 'WorldUnits', 'millimeters', ...
        'ImageSize', [mrows, ncols]);
    
    % View reprojection errors
    h1=figure; showReprojectionErrors(cameraParams);
    
    % Visualize pattern locations
    h2=figure;
    tvecs = showExtrinsicsV2(cameraParams, 'PatternCentric');

    % Store translation vectors
    tvec1 = tvecs(:,1);
    tvec2 = tvecs(:,2);
    tvec3 = tvecs(:,3);
    
    % Store rotation matrices
    rmtx1 = cameraParams.RotationMatrices(:,:,1);
    rmtx2 = cameraParams.RotationMatrices(:,:,2);
    rmtx3 = cameraParams.RotationMatrices(:,:,3);

    % Print translation vectors and rotation matrices
    disp('Camera 1 :')
    disp('Translation vector :')
    disp(tvec1)
    disp('Rotation matrix :')
    disp(rmtx1)

    disp('Camera 2 :')
    disp('Translation vector :')
    disp(tvec2)
    disp('Rotation matrix :')
    disp(rmtx2)

    disp('Camera 3 :')
    disp('Translation vector :')
    disp(tvec3)
    disp('Rotation matrix :')
    disp(rmtx3)

    % Wait for keyboard event before ending function
    while(waitforbuttonpress ~= 1)
    end
end