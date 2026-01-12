function expID = newExpID(animal)
remoteRepos = remotePath;
formatOut = 'yyyy-mm-dd';
currentDate = datestr(now,formatOut);

% check if animal already exists
if ~exist(fullfile(remoteRepos,animal),'dir')
    % if not make it
    mkdir(fullfile(remoteRepos,animal));
end
    
% iterate through possible expIDs until a non-existant one is found
baseExpNumber = 0;
while true
    baseExpNumber = baseExpNumber + 1;
    % format number to be trailed with zeros if needed
    if baseExpNumber<10
        baseExpNumberStr = ['0',num2str(baseExpNumber)];
    else
        baseExpNumberStr = num2str(baseExpNumber);
    end
    % check if it exists
    possibleExpID = [currentDate,'_',baseExpNumberStr,'_',animal];
    if ~exist(fullfile(remoteRepos,animal,possibleExpID),'dir')
        % if not make it
        expID = possibleExpID;
        mkdir(fullfile(remoteRepos,animal,possibleExpID));
        break;
    end
end
end